# syntax=docker/dockerfile:1

ARG TARGETARCH
ARG VERSION

FROM docker.io/library/python:3.14-slim@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=65534:65534 bridge /app/bridge

USER 65534:65534

# Healthcheck validates the runtime can import the bridge package. This is a
# no-op for the CronJob deployment (containers exit per invocation) but catches
# image regressions early and is required if the bridge ever runs as a long
# lived Deployment for continuous reconciling.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import bridge; print('ok')"

ENTRYPOINT ["python", "-m", "bridge.main"]
