# Prometheus Gateway for the IKEA Dirigera Gateway

[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/ajabep/dirigera-prometheus/badge)](https://securityscorecards.dev/viewer/?uri=github.com/ajabep/dirigera-prometheus)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=ajabeporg_dirigera-prometheus&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=ajabeporg_dirigera-prometheus)


## ⚠️ Known Issues

Before using this project, please, acknowledge the following **MAJOR** issues. I left this project in this shaky state because it was fine enough for my needs.

Of course, you're welcome to provide merge request to fix them!

### ⚠️ Very ugly code base and slow HTTP server! ⚠️

I know that the code base is ugly and the HTTP server sooooooo slow.

I faced the following issue:

When I subscribe updates from the Dirigera Gateway, Python stays at this step without opening (at that step) the HTTP.

Thus, I tried multi-threading, but the Prometheus sharing between processes and thread is really poor and was not retrieving all the data.

I began to rewrote everything in async, hoping it will solve all the problems: if I correctly understood the documentation, the event loop will schedule the different task in "parallel" in the same thread. Thus, we can run the update handling and the HTTP request handling in the same thread, resolving all the previous issue. TBH, for this part, any idea and/or design (or even confirm that it could work) is more than welcome! I have absolutely zero knowledge about the asyncio part of Python.

But, unfortunately, I faced a lack of time. So, I just adapted a bit the code to make it working, even if it's soooooo slow.

### Not everything is implemented

Due to the dirigera Python dependency, not everything is implemented. I hope to be able to fix that later, but, it's 
not really my current priority.

### Only tested with my setup

This has only been tested with my setup. I do not own every IKEA devices (a lot, but definitely not all), so, some of them may miss some metrics or may do this code bugging.

Please, if you find a bug, report it! Thus, everyone may be able to have a fix... including you!

### Metrics are not really OpenTelemetry compliant

Yes. I know, I do not use here the label and all of these things. I know. Once again, it's "good enough" for me, and 
I miss time.

## How to build?

Build the docker container `./Dockerfile` to build all the system. Refers to the installation part to know how to use it.

## Installation

To install this software, you have to use the container. To install the container, here are the instructions:

1. Pull the docker image [`ghcr.io/ajabep/dirigera-prometheus:main`](https://ghcr.io/ajabep/dirigera-prometheus:main);
2. Issue an authentication token to the Dirigera hub by:
   1. Execute `docker run --rm -it ghcr.io/ajabep/dirigera-prometheus:main generateToken`;
   2. Follow instructions (pressing the button of the hub and press enter);
3. The container has to be linked to the Dirigera hub;
4. The environment variables are to are the following.
	- `REMOTE`: (string; an IP or a domain name) The address of the Dirigera hub;
	- `HOST`: (string; an IP or a domain name) The hostname that requests are supposed to use. Add the port number
	  is not standard;
	- `DIRIGERA_TOKEN`: (string; a JWT token) The token issued previously, at step 2;
	- `WEBPATH`: (Optional; string; a path) The path to use to access this service (in case it's behind a reverse proxy). This may
      be used to avoid exposing the prometheus at a predictable endpoint, but is not a strong authentication;
    - `VERBOSE`: (Optional; positive integer) When used, the logs will be verbose;
    - `DO_NOT_VERIFY_REVERSE_PROXY`: (Optional; string) When the string is `The reverse proxy set X-Forwarded-For and
	  X-Forwarded-Host headers`, the launch script will not verify if the reverse proxy putted in front of this app is
	  well configured. This is useful when your docker container is not able to reach your reverse proxy, if your
	  website has no HTTPS or even for development purpose.
	- `UNSAFE_DEVELOPMENT_MODE`: (Optional; string) **UNSAFE** to use only when you are developing. If the value is not
	  the right one (embedded in the entrypoint file ; case-sensitive), the dev mode will not be enabled. Please, make
	  sure this is used only in a development network and computer. This will only make this container more weak and
	  vulnerable. If you want to have some verbose log, use the `VERBOSE` variable. This option is **UNSAFE**. Do
      **NOT** use it.
5. You HAVE to put this server BEHIND a reverse-proxy. For more info, refer to the
   [Flask documentation](https://flask.palletsprojects.com/en/2.3.x/deploying/). Your reverse proxy **HAVE** to set the
   headers `X-Forwarded-For`, `X-Forwarded-Host`. If you don't want to use them, please, clear them.

The application files are putted in the `/app` directory.

Logs are available on the STDOUT.

## TODO

Check issues and fix some of them!

Or check the "Known Issues" section of this file.

## License

*dirigera-prometheus-gateway* is released under the Unlicensed license. See the [./LICENSE](LICENSE) file.
