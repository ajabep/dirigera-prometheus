FROM alpine:3.24.4@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf

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
