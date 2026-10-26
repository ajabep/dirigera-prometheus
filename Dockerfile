FROM alpine:3.24.3@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a

COPY --chown=root:root ./src/ /app/
COPY --chown=root:root ./docker/entrypoint.sh /
COPY --chown=root:root ./docker/healthcheck.sh /
WORKDIR /app
ENV PROMETHEUS_MULTIPROC_DIR=/tmp

RUN apk add --update --no-cache python3~3.14 \
                                poetry~2 \
                                curl~8 \
 && chown root:root /app \
 && adduser -S -D appuser \
 && chmod 0555 /app/app.py /entrypoint.sh /healthcheck.sh

# hadolint ignore=DL3066
USER appuser
RUN poetry install

EXPOSE 8080
HEALTHCHECK --interval=1m --timeout=30s --start-period=5s --retries=3 CMD ["/healthcheck.sh"]

ENTRYPOINT [ "/entrypoint.sh" ]
