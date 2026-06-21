FROM stalwartlabs/stalwart:v0.16

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY bootstrap.py /opt/smartbox/bootstrap.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /opt/smartbox/bootstrap.py
USER stalwart

ENTRYPOINT ["/entrypoint.sh"]
