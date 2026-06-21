FROM stalwartlabs/stalwart:v0.16

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY services/stalwart/bootstrap.py /opt/smartbox/bootstrap.py
COPY services/stalwart/entrypoint.sh /entrypoint.sh
COPY config/stalwart/config.json /etc/stalwart/config.json
RUN chmod +x /entrypoint.sh /opt/smartbox/bootstrap.py
USER stalwart

ENTRYPOINT ["/entrypoint.sh"]
