#!/usr/bin/env python3
"""dirigera-prometheus is a simple and silly web server to expose the state and metrics of
accessories managed by a Dirigera hub to Prometheus.
"""
import argparse
import dataclasses
import datetime
import enum
import json
import logging
import os
import pathlib
import pprint
import secrets
import string
import sys
import textwrap
import threading
import typing
from functools import cache
from threading import Thread
from typing import Callable

import dirigera
import dirigera.devices.air_purifier
import dirigera.devices.base_ikea_model
import dirigera.devices.blinds
import dirigera.devices.controller
import dirigera.devices.device
import dirigera.devices.environment_sensor
import dirigera.devices.light
import dirigera.devices.motion_sensor
import dirigera.devices.open_close_sensor
import dirigera.devices.outlet
import dirigera.devices.scene
import dirigera.devices.water_sensor
import dirigera.hub.hub
import pydantic
import requests
from flask import Flask, Blueprint, request, Response, abort
from prometheus_client import make_wsgi_app, Counter, Gauge, Info, Histogram, Enum, Summary, \
    multiprocess, CollectorRegistry, REGISTRY
from prometheus_client.metrics import MetricWrapperBase
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix

APP_DIR = pathlib.Path(__file__).absolute().parent
DEFAULT_ADDRESS = '0.0.0.0'  # nosec: disable=104
DEFAULT_PORT = 8080
DEFAULT_PROTO = 'http'
CONFIG = {}

MYREGISTRY = CollectorRegistry()

i = Info("dirigera_prometheus_gateway", __doc__.replace('\n', '').strip())
metric_export = Histogram('metric_export_seconds', 'Histogram of the metrics generation')

bp = Blueprint('app', __name__)

@bp.before_request
def force_hostname():
    """Force the usage of the right hostname"""
    if request.host != CONFIG['HOSTNAME']:
        logging.warning("Just seen a request asking for '%s', expecting the hostname '%s'",
                        request.host, CONFIG['HOSTNAME'])
        abort(404)

@bp.after_request
def security_headers(response: Response) -> Response:
    """Setup some security headers if not already present"""
    # pylint: disable=line-too-long
    headers = {
        'Content-Security-Policy': "default-src 'none'; "
                                   "base-uri 'none'; "
                                   "sandbox ; "
                                   "form-action 'none'; "
                                   "frame-ancestors 'none'; "
                                   "upgrade-insecure-requests; "
                                   "require-trusted-types-for 'script'; "
                                   "trusted-types 'none'",
        'X-Content-Type-Options': 'nosniff',
        'Referer': 'no-referrer',
        'Permissions-Policy': 'accelerometer=(), ambient-light-sensor=(), autoplay=(), '
                              'battery=(), camera=(), display-capture=(), document-domain=(), '
                              'encrypted-media=(), execution-while-not-rendered=(), '
                              'execution-while-out-of-viewport=(), fullscreen=(), gamepad=(), '
                              'geolocation=(), gyroscope=(), hid=(), identity-credentials-get=(), '
                              'idle-detection=(), local-fonts=(), magnetometer=(), microphone=(), '
                              'midi=(), payment=(), picture-in-picture=(), '
                              'publickey-credentials-create=(), publickey-credentials-get=(), '
                              'screen-wake-lock=(), serial=(), speaker-selection=(), '
                              'storage-access=(), usb=(), web-share=(), xr-spatial-tracking=()'
    }
    for h_name, h_value in headers.items():
        if response.headers.get(h_name) is None:
            response.headers[h_name] = h_value
    return response

@bp.get("/robots.txt")
def robotstxt():
    """Robots.txt handler/generator"""
    logging.debug("ROBOTS: Thread ident = %r", threading.get_ident())
    return Response(
        textwrap.dedent(
            # pylint: disable=line-too-long
            '''\
            # Stop all search engines from crawling this site
            User-agent: *
            Disallow: /
            '''
        ),
        mimetype='text/plain',
        content_type='text/plain; charset=utf-8'
    )

@bp.get("/.well-known/security.txt")
def securitytxt():
    """Security.txt handler/generator"""
    return Response(
        textwrap.dedent(
            # pylint: disable=line-too-long
            '''\
            Contact: https://github.com/ajabep/dirigera-prometheus/blob/main/SECURITY.md
            Expires: 2025-12-31T23:00:00.000Z
            Acknowledgments: https://github.com/ajabep/dirigera-prometheus/blob/main/SECURITY.md#hall-of-fame
            Preferred-Languages: en, fr
            '''
        ),
        mimetype='text/plain',
        content_type='text/plain; charset=utf-8'
    )

def get_hub() -> dirigera.Hub:
    return dirigera.Hub(
        token=CONFIG['TOKEN'],
        ip_address=CONFIG['REMOTE_ADDR']
    )

def snakecase(s: str) -> str:
    return ''.join([
        c if c in string.ascii_lowercase + string.digits else '_' + c.lower() if c in string.ascii_uppercase else '_'
        for c in s
    ]).strip('_').replace('__', '_')

T = typing.TypeVar('T')
def str_to_type(value: str, dest_type: type[T]) -> T:
    if dest_type == bool:
        return value.lower() in ['true', 't', 'yes', 'y']
    if dest_type in [datetime.datetime, datetime.time, datetime.date]:
        dest_type = dest_type.fromisoformat
    return dest_type(value)

def any_to_type(value: typing.Any, dest_type: type[T]) -> T:
    if isinstance(value, dest_type):
        return value
    if not isinstance(value, str):
        value = str(value)
    return str_to_type(value, dest_type)

@dataclasses.dataclass
class DeviceMetric:
    attributes: Info = dataclasses.field(init=False)
    values: typing.Dict[str, MetricWrapperBase] = dataclasses.field(default_factory=dict, init=False)
    dev: dirigera.devices.device.Device

    def __eq__(self, other) -> bool:
        return self.dev.id == other.dev.id

    def __hash__(self) -> int:
        return hash(self.dev.id)

    @cache
    def get_own_attributes(self) -> typing.Dict[str, pydantic.fields.FieldInfo]:
        my_fields = self.dev.attributes.model_fields
        attrs = set(my_fields.keys())
        default_attrs = set(dirigera.devices.device.Attributes.model_fields.keys())
        return {
            k: my_fields[k]
            for k in attrs - default_attrs
        }

    def __init__(self, dev: dirigera.devices.device.Device):
        logging.info('Init a DeviceMetric with %r', dev)
        self.dev = dev
        logging.debug('name=%s', self.name)
        dev_id = dev.id
        dev_type = dev.device_type
        self.attributes = Info(self.name + '_attributes', f'Accessory named "{self.name}", id "{dev_id}"')
        self.values = {}
        for attr_name, field_info in self.get_own_attributes().items():
            f_type = field_info.annotation

            if isinstance(f_type, type(typing.Optional[str])) and f_type.__origin__ == typing.Union:
                if f_type.__args__[0] is not None:
                    f_type = f_type.__args__[0]
                else:
                    f_type = f_type.__args__[0]

            logging.debug(f_type)
            logging.debug(type(f_type))
            if f_type in [int, float]:
                self.values[attr_name] = Gauge(self.name + '_' + attr_name, f'Values related to the {dev_type} accessory {self.name} ({dev_id})')
            elif issubclass(f_type, enum.Enum):
                self.values[attr_name] = Enum(self.name + '_' + attr_name, f'Values related to the {dev_type} accessory {self.name} ({dev_id})',
                                              states=[
                                                  str(e)
                                                  for e in f_type
                                              ])
            elif f_type == bool:
                self.values[attr_name] = Enum(self.name + '_' + attr_name, f'Values related to the {dev_type} accessory {self.name} ({dev_id})',
                                              states=['False', 'True'])
            elif f_type in [str, datetime.time, datetime.datetime]:
                self.values[attr_name] = Info(self.name + '_' + attr_name, f'Values related to the {dev_type} accessory {self.name} ({dev_id})')
            else:
                raise NotImplementedError(f'Cannot handle field type {f_type}')

        self.autofill()

    def __del__(self):
        self.attributes.clear()
        self.values.clear()

    @property
    def name(self) -> str:
        room = self.dev.room
        prefix = ''
        if room is not None:
            prefix = snakecase(room.name) + '_'
        return prefix + snakecase(self.dev.attributes.custom_name)

    @classmethod
    def to_str(cls, v: typing.Any) -> str:
        return str(v) if v is not None else ''

    @classmethod
    def to_dict_str(cls, d: typing.Dict[str, typing.Any]) -> typing.Dict[str, str]:
        return {
            k: cls.to_str(v)
            for k, v in d.items()
        }

    def autofill(self) -> None:
        self.attributes.info(self.to_dict_str({
            'id': self.dev.id,
            'type': self.dev.type,
            'device_type': self.dev.device_type,
            'is_reachable': self.dev.is_reachable,
            'remote_links': len(self.dev.remote_links),
            'is_hidden': self.dev.is_hidden,
            'capabilities_receive': len(self.dev.capabilities.can_receive),
            'capabilities_send': len(self.dev.capabilities.can_send),

            'custom_name': self.dev.attributes.custom_name,
            'model': self.dev.attributes.model,
            'manufacturer': self.dev.attributes.manufacturer,
            'firmware_version': self.dev.attributes.firmware_version,
            'serial_number': self.dev.attributes.serial_number,
            'product_code': self.dev.attributes.product_code,
            'ota_status': self.dev.attributes.ota_status,
            'ota_state': self.dev.attributes.ota_state,
            'ota_progress': self.dev.attributes.ota_progress,
            'ota_policy': self.dev.attributes.ota_policy,
            'ota_schedule_start':  self.dev.attributes.ota_schedule_start,
            'ota_schedule_end': self.dev.attributes.ota_schedule_end,

            'room_id': self.dev.room.id,
            'room_name': self.dev.room.name,
            'room_color': self.dev.room.color,
            'room_icon': self.dev.room.icon,
        }))
        for attr_name, _ in self.get_own_attributes().items():
            value_obj = self.values[attr_name]
            value_to_set = getattr(self.dev.attributes, attr_name)

            if isinstance(value_obj, Counter):
                raise NotImplementedError(f'Cannot set a prometheus Counter without resetting it (value {value_obj})')
            if isinstance(value_obj, Info):
                value_obj.info({'value': str(value_to_set)})
            if isinstance(value_obj, Gauge):
                if value_to_set is not None:
                    value_obj.set(value_to_set)
            if isinstance(value_obj, (Summary, Histogram)):
                if value_to_set is not None:
                    value_obj.observe(value_to_set)
            if isinstance(value_obj, Enum):
                if value_to_set is not None:
                    value_obj.state(str(value_to_set))

    def update_obj(self, obj: dirigera.devices.base_ikea_model.BaseIkeaModel, data: typing.Dict[str, typing.Any]) -> None:
        for k, v in data.items():
            k = snakecase(k)
            logging.debug('Updating key %r', k)
            if not hasattr(obj, k):
                logging.error('TODO? %s, %s', k, type(obj))
                continue

            attr = getattr(obj, k)
            should_be_dict = isinstance(attr, dirigera.devices.base_ikea_model.BaseIkeaModel)
            is_dict = isinstance(v, dict)
            if should_be_dict ^ is_dict:
                raise TypeError(f'Unexpected presence or absence of a dict: trying to access {k} of {type(obj)} with value {v:r}')

            if should_be_dict:
                logging.debug('Is a Dict!')
                self.update_obj(attr, v)
                logging.debug('End of the Dict')
            else:
                logging.debug('Updating to %r', type(attr))
                setattr(obj, k, any_to_type(v, type(attr)))

    def update_from_dict(self, data: typing.Dict[str, typing.Any]) -> None:
        self.update_obj(self.dev, data)
        self.autofill()

devices : typing.Dict[str, DeviceMetric] = {}
devices_counter = Gauge('devices_counter', documentation='The total number of devices registered to the python script')
devices_counter.set_function(lambda: len(devices))
general_metrics = {
    'updates': Counter('updates_total', documentation='The total number of updates since the last reboot of this system'),
    'ws_failures': Counter('ws_failures_total', documentation='The total number of failures since the last reboot of this system'),
    'devices ': devices_counter,
}

def dict_to_device_metric(dev_dict: typing.Dict[str, typing.Any]) -> DeviceMetric:
    if 'type' not in dev_dict:
        raise ValueError('Dict do not have a type key')
    t = dev_dict['type']
    fnc_name = f'dict_to_{snakecase(t)}'
    if not hasattr(dirigera.hub.hub, fnc_name):
        raise ValueError(f'dirigera.hub.hub do not have a function to create a {t} from a dict. Expected {fnc_name}')
    fnc = getattr(dirigera.hub.hub, fnc_name)
    dev = fnc(dev_dict)
    return DeviceMetric(dev)


# noinspection PyUnusedLocal
def populate_data_on_message(ws: typing.Any, message: str):
    logging.debug("populate_data_on_message ; Thread ident = %r", threading.get_ident())
    general_metrics['updates'].inc()
    x = None
    try:
        x = json.loads(message)
    except json.decoder.JSONDecodeError as e:
        logging.exception('Incorrect JSON message: %s', message, exc_info=e)
        return
    logging.debug("x=%s", pprint.pformat(x))

    if x['source'] == 'urn:com:ikea:homesmart:iotc:timeservice':
        # Raised when the location of the dirigera hub is changed. This location is used for the sunrise and sunshine hours.
        return
    if x['source'] == 'urn:com:ikea:homesmart:iotc:rulesengine':
        # Raised when a device is linked or unlinked to another one, and when a scene is created or deleted.
        return
    if x['source'] == 'hub':
        # Raised when a room is created, updated (?) and deleted
        return
    if x['source'] == 'urn:com:ikea:homesmart:iotc:tagmanager':
        # Raised when ???????
        return
    # 'urn:com:ikea:homesmart:iotc:iotcd': Raised when a device is removed or ????
    # 'urn:com:ikea:homesmart:iotc:zigbee': Raised when a device have a different, and other things regarding the

    if x['type'] not in [
        'deviceRemoved',
        'deviceStateChanged',
        'deviceAdded',
        'deviceConfigurationChanged'
    ]:
        return

    if x['type'] == 'deviceAdded':
        try:
            dm = dict_to_device_metric(x['data'])
        except ValueError as e:
            logging.exception('Error while loading a device metric from a dict', exc_info=e)
            return
        dev_id = dm.dev.id
        devices[dev_id] = dm
        devices_counter.inc()
        return


    xdata = x['data']
    if 'id' not in xdata:
        logging.error('Unexpected miss of the id key in the data part of a received message: %s', message)
        return
    target_id = xdata['id']

    if x['type'] == 'deviceRemoved':
        del devices[target_id]
        devices_counter.dec()
        return

    logging.debug("target_id=%r", target_id)
    logging.debug("devices[target_id] = %s", pprint.pformat(devices.get(target_id)))
    dev = devices[target_id]
    try:
        dev.update_from_dict(xdata)
    except TypeError as e:
        logging.exception(e.args, exc_info=e)

# noinspection PyUnusedLocal
def populate_data_on_error(ws: typing.Any, *args, **kwargs):
    logging.debug("populate_data_on_error ; Thread ident = %r ; args = %r, kwargs=%r", threading.get_ident(), args, kwargs)
    general_metrics['ws_failures'].inc()

def populate_data(hub: dirigera.Hub) -> None:
    try:
        devs = hub.get_all_devices()
    except requests.exceptions.HTTPError:
        raise Exception("The Authentication Token is no longer valid")
    except requests.exceptions.ConnectTimeout:
        raise Exception("The Dirigera hub is not reachable")

    logging.error("populate_data ; PID=%r", os.getpid())
    for dev in devs:
        devices[dev.id] = DeviceMetric(dev)

    hub.create_event_listener(
        on_message=populate_data_on_message,
        on_error=populate_data_on_error,
    )

def main():
    """Parse CLI arguments and start the server"""
    logging.error("PID=%r", os.getpid())
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--url", help="URI behind which the web page is available")
    parser.add_argument("-v", "--verbose", help="Log HTTP requests", action="count", default=0)
    parser.add_argument(
        "--unsafe-development-mode",
        help="UNSAFE; Enable the development mode. DO NOT USE THIS IN PRODUCTION",
        action="store_true",
        default=False,
        dest="devmode"
    )
    parser.add_argument(
        "--webpath",
        help="If behind a reverse proxy, is the path to use to access this service.",
        default=""
    )
    parser.add_argument("remote", help="Address of the Dirigera hub")
    parser.add_argument("hostname", help="The hostname that requests are supposed to use")
    parser.add_argument("token", help="The authentication token issued by the Dirigera hub")

    # Parse arguments
    args = parser.parse_args()

    print(f'Verbosity: {args.verbose}')
    if args.verbose >= 2:
        logging.basicConfig(level=logging.NOTSET)
    elif args.verbose == 1:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    CONFIG['REMOTE_ADDR'] = args.remote
    CONFIG['HOSTNAME'] = args.hostname
    CONFIG['TOKEN'] = args.token

    webpath = args.webpath.replace('\\', '/')
    webpath = '/' + webpath.strip('/')

    hub = get_hub()
    try:
        hub.get_scenes()
    except requests.exceptions.HTTPError as e:
        raise Exception("The Authentication Token is not valid") from e
    except requests.exceptions.ConnectTimeout as e:
        raise Exception("The Dirigera hub is not reachable") from e
    global_watcher = Thread(target=populate_data, name='Dirigera Watcher', args=[hub], daemon=True)
    global_watcher.start()

    app = Flask(__name__)
    app.secret_key = secrets.token_hex()
    app.register_blueprint(bp, url_prefix=webpath)

    logging.info("Listening on: %s://%s:%s%s", DEFAULT_PROTO, DEFAULT_ADDRESS, DEFAULT_PORT, webpath)
    if args.url is not None:
        logging.info("If your redirection works correctly, it should be available using: %s",
                     args.url)

    # Add prometheus wsgi middleware to route /metrics requests
    #multiprocess.MultiProcessCollector(MYREGISTRY)
    #MYREGISTRY.register(REGISTRY)
    def metric_factory() -> Callable[[typing.Dict, Callable], typing.List[bytes]]:
        logging.error("FACTORY")
        f = make_wsgi_app(REGISTRY)
        def g(*args, **kwargs):
            logging.error("PID=%r", os.getpid())
            return f(*args, **kwargs)
        return g
    metric_path = '/metrics'
    if webpath != '/':
        metric_path = webpath + metric_path
    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {
        metric_path: metric_export.time()(metric_factory())
    })

    if args.devmode:
        CONFIG['DEVMODE'] = True
        app.run(
            debug=args.verbose >= 1,
            host=DEFAULT_ADDRESS,
            port=DEFAULT_PORT
        )
    else:
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=1, x_host=1
        )
    return app

def create_app(argv) -> Flask:
    """Create the right app object for WSGI server, and transforms the CLI arguments given as an
    argument to sys.argv"""
    sys.argv = argv.split(' ')
    return main()

if __name__ == "__main__":
    main()
