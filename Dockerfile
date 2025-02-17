FROM alpine:3.21.3@sha256:a8560b36e8b8210634f77d9f7f9efd7ffa463e380b75e2e74aff4511df3ef88c

COPY --chown=root:root ./src/ /app/
COPY --chown=root:root ./docker/entrypoint.sh /
WORKDIR /app
ENV PROMETHEUS_MULTIPROC_DIR=/tmp

RUN apk add --update --no-cache python3~3.12 \
                            	poetry~1.8 \
                                curl~8 \
 && chown root:root /app \
 && adduser -S -D appuser \
 && chmod 0555 /app/app.py /entrypoint.sh

USER appuser
RUN poetry install

EXPOSE 8080
HEALTHCHECK --interval=1m --timeout=30s --start-period=5s --retries=3 CMD curl -H 'Host: '"$HOST" http://127.0.0.1:8080"$WEBPATH"/metrics -f

ENTRYPOINT [ "/entrypoint.sh" ]
