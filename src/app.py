#!/usr/bin/env python3
"""dirigera-prometheus is a simple and silly web server to expose the state and metrics of
accessories managed by a Dirigera hub to Prometheus.
"""
import argparse
import dataclasses
import datetime
import enum
import logging
import pathlib
import secrets
import string
import sys
import textwrap
import threading
import typing
from functools import cache
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
import requests
from flask import Flask, Blueprint, request, Response, abort
from prometheus_client import make_wsgi_app, Counter, Gauge, Info, Histogram, CollectorRegistry
from prometheus_client.metrics import MetricWrapperBase, Enum, Summary
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix


APP_DIR = pathlib.Path(__file__).absolute().parent
DEFAULT_ADDRESS = '0.0.0.0'  # nosec: disable=104
DEFAULT_PORT = 8080
DEFAULT_PROTO = 'http'
CONFIG = {}

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

def test_hub_params() -> None:
    hub = get_hub()
    hub.get_scenes()

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
    values: dict[str, MetricWrapperBase] = dataclasses.field(default_factory=dict, init=False)
    dev: dirigera.devices.device.Device

    def __eq__(self, other) -> bool:
        return self.dev.id == other.dev.id

    def __hash__(self) -> int:
        return hash(self.dev.id)

    @cache  # pylint: disable=method-cache-max-size-none
    def get_own_attributes(self) -> dict[str, object]:
        my_fields = self.dev.attributes.model_fields
        attrs = set(my_fields.keys())
        default_attrs = set(dirigera.devices.device.Attributes.model_fields.keys())
        return {
            k: my_fields[k]
            for k in attrs - default_attrs
        }

    def __init__(self, dev: dirigera.devices.device.Device, registry: CollectorRegistry):
        logging.info('Init a DeviceMetric with %r', dev)
        self.dev = dev
        logging.debug('name=%s', self.name)
        dev_id = dev.id
        dev_type = dev.device_type
        self.registry = registry
        self.attributes = Info(self.name + '_attributes',
                               f'Accessory named "{self.name}", id "{dev_id}"',
                               registry=self.registry)
        self.values = {}

        for attr_name, field_info in self.get_own_attributes().items():
            f_type = field_info.annotation

            if isinstance(f_type, type(typing.Optional[str])) and f_type.__origin__ == typing.Union:
                if f_type.__args__[0] is not None:
                    f_type = f_type.__args__[0]
                else:
                    f_type = f_type.__args__[0]

            if f_type in [int, float]:
                self.values[attr_name] = Gauge(self.name + '_' + attr_name, f'Values related to the {dev_type} accessory {self.name} ({dev_id})', registry=self.registry)
            elif issubclass(f_type, enum.Enum):
                self.values[attr_name] = Enum(self.name + '_' + attr_name, f'Values related to the {dev_type} accessory {self.name} ({dev_id})',
                                              states=[
                                                  str(e)
                                                  for e in f_type
                                              ],
                                              registry=self.registry)
            elif f_type == bool:
                self.values[attr_name] = Enum(self.name + '_' + attr_name, f'Values related to the {dev_type} accessory {self.name} ({dev_id})',
                                              states=['False', 'True'], registry=self.registry)
            elif f_type in [str, datetime.time, datetime.datetime]:
                self.values[attr_name] = Info(self.name + '_' + attr_name, f'Values related to the {dev_type} accessory {self.name} ({dev_id})', registry=self.registry)
            else:
                raise NotImplementedError(f'Cannot handle field type {f_type}')

        self.autofill()

    def __del__(self):
        self.unregister()

    def unregister(self):
        self.registry.unregister(self.attributes)
        for value in self.values.values():
            self.registry.unregister(value)

    @property
    def name(self) -> str:
        room = self.dev.room
        prefix = ''
        if room is not None:
            prefix = snakecase(room.name) + '_'
        return prefix + snakecase(self.dev.attributes.custom_name) + '_' + snakecase(self.dev.device_type)

    @classmethod
    def to_str(cls, v: typing.Any) -> str:
        return str(v) if v is not None else ''

    @classmethod
    def to_dict_str(cls, d: dict[str, typing.Any]) -> dict[str, str]:
        return {
            k: cls.to_str(v)
            for k, v in d.items()
        }

    def autofill(self) -> None:
        parameters = {
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
        }
        if self.dev.room is not None:
            parameters.update({
                'room_id': self.dev.room.id,
                'room_name': self.dev.room.name,
                'room_color': self.dev.room.color,
                'room_icon': self.dev.room.icon,
            })

        self.attributes.info(self.to_dict_str(parameters))
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

    def update(self, dev: dirigera.devices.device.Device):
        if self.dev.id != dev.id:
            raise AssertionError("Have to update metrics based on another device??")
        self.dev = dev
        self.autofill()

class DeviceRegistry:
    devices : dict[str, DeviceMetric] = {}
    def __init__(self):
        self.registry = CollectorRegistry()
        self.devices_counter = Gauge('devices_counter', documentation='The total number of devices registered to the python script', registry=self.registry)
        self.devices_counter.set_function(lambda: len(self.devices))

        Info("dirigera_prometheus_gateway", __doc__.replace('\n', '').strip(), registry=self.registry)
        self.metric_export = Histogram('metric_export_seconds', 'Histogram of the metrics generation', registry=self.registry)

        self.hub = get_hub()
        self.update()

    def metric_factory(self) -> Callable:
        prometheus_display_metric = make_wsgi_app(self.registry)

        def update_n_display(*args, **kwargs):
            self.update()
            return prometheus_display_metric(*args, **kwargs)

        return self.metric_export.time()(
            update_n_display
        )

    def update(self):
        try:
            devs = self.hub.get_all_devices()
        except requests.exceptions.HTTPError as exc:
            raise Exception("The Authentication Token is no longer valid") from exc
        except requests.exceptions.ConnectTimeout as exc:
            raise Exception("The Dirigera hub is not reachable") from exc

        new_dev_id = set()
        old_dev_id = list(self.devices.keys())
        for dev in devs:
            new_dev_id.add(dev.id)
            if self.devices.get(dev.id) is None:
                self.devices[dev.id] = DeviceMetric(dev, registry=self.registry)
            else:
                self.devices[dev.id].update(dev)

        logging.debug("new_dev_id=%r", new_dev_id)
        logging.debug("old_dev_id=%r", old_dev_id)
        for old_id in old_dev_id:
            if old_id not in new_dev_id:
                logging.debug("old_id=%s", old_id)
                self.devices[old_id].unregister()
                del self.devices[old_id]

def main():
    """Parse CLI arguments and start the server"""
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

    try:
        test_hub_params()
    except requests.exceptions.HTTPError as e:
        raise Exception("The Authentication Token is not valid") from e
    except requests.exceptions.ConnectTimeout as e:
        raise Exception("The Dirigera hub is not reachable") from e

    webpath = args.webpath.replace('\\', '/')
    webpath = '/' + webpath.strip('/')

    app = Flask(__name__)
    app.secret_key = secrets.token_hex()
    app.register_blueprint(bp, url_prefix=webpath)

    logging.info("Listening on: %s://%s:%s%s", DEFAULT_PROTO, DEFAULT_ADDRESS, DEFAULT_PORT, webpath)
    if args.url is not None:
        logging.info("If your redirection works correctly, it should be available using: %s",
                     args.url)

    # Add prometheus wsgi middleware to route /metrics requests
    dev_reg = DeviceRegistry()
    metric_path = '/metrics'
    if webpath != '/':
        metric_path = webpath + metric_path
    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {
        metric_path: dev_reg.metric_factory(),
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
