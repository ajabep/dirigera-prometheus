#!/bin/sh
set -e

rage_quit() {
	echo "CRITICAL ERROR: $*"
	echo "CRITICAL ERROR: $*" >&2
	exit 1
}

verifyRemote() {
	if [ "$REMOTE" = "" ]
	then
		rage_quit "To run this in a docker container, you have to provide the IP of the Dirigera hub."
	fi
	ping -c 1 "$REMOTE" || rage_quit "Cannot reach the Dirigera hub."
}

# If we want to generate a token
if [ "$#" -gt 0 ]
then
	if [ "$1" = "sh" ]
	then
		exec /bin/ash
	elif [ "$1" = "python" ]
	then
		exec poetry run python
	elif [ "$1" = "generateToken" ]
	then
		verifyRemote
		exec poetry run generate-token "$REMOTE"
	fi
fi

# Let's start the serve!
set --

if [ "$VERBOSE" != "" ]
then
    set -- "$@" -v

    if [ "$VERBOSE" -gt 1 ]
    then
        set -- "$@" -v
    fi
    if [ "$VERBOSE" -gt 2 ]
    then
        set -- "$@" -v
    fi
fi

verifyRemote

if [ "$HOST" = "" ]
then
    rage_quit "To run this in a docker container, you have to provide the hostname."
else
	if [ "X$DO_NOT_VERIFY_REVERSE_PROXY" != "XThe reverse proxy set X-Forwarded-For and X-Forwarded-Host headers" ]
	then
		tmpfile="$(mktemp)"
		printf 'HTTP/1.1 200 OK\n\n\n' | nc -lvp 8080 >"$tmpfile" &
		echo "Testing the reverse proxy"
		curl -k https://"$HOST/$WEBPATH" || rage_quit "We cannot verify the reverse proxy: curl exited with code $?. Quitting"
		grep -q 'X-Forwarded-For:' "$tmpfile" || rage_quit "It seems that the reverse proxy is not proper configured: can't find X-Forwarded-For in the request!"
		grep -q 'X-Forwarded-Host:' "$tmpfile" || rage_quit "It seems that the reverse proxy is not proper configured: can't find X-Forwarded-Host in the request!"
	fi
fi

if [ "$DIRIGERA_TOKEN" = "" ]
then
    rage_quit "To run this in a docker container, you have to provide an authentication token issued by the Dirigera hub."
else
	if [ "X$(echo "$DIRIGERA_TOKEN" | sed 's/eyJ[a-zA-Z0-9]\+\.eyJ[a-zA-Z0-9]\+\.[a-zA-Z0-9]\+//')" = "X" ]
	then
		rage_quit "To run this in a docker container, you have to provide a VALID authentication token issued by the Dirigera hub."
	fi
fi


if [ "X$WEBPATH" != "X" ]
then
	set -- "$@" --webpath "$WEBPATH"
fi

set -- "$@" "$REMOTE" "$HOST" "$DIRIGERA_TOKEN"

if [ "$UNSAFE_DEVELOPMENT_MODE" = "This is UNSAFE and I want to make this server more vulnerable, PLease, TrUST me, I reALly reaLLY wanT to Be haCKed!" ]
then
	echo "WARNING! YOU ENABLED THE DEVELOPMENT MODE. THUS, THIS APPLICATION WILL BE MUCH MORE VULNERABLE!"
    set -- "$@" --unsafe-development-mode
	# shellcheck disable=SC2048,SC2086
    exec poetry run python /app/app.py "$@"
fi

exec poetry run gunicorn --preload --reuse-port --group nogroup --proxy-allow-from '*' --bind 0.0.0.0:8080 \
                         --workers 1 "app:create_app('$*')"
