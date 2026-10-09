#!/bin/sh

curl -f -H "Host:$HOST" "http://127.0.0.1:8080$WEBPATH/metrics"