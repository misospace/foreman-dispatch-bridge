# syntax=docker/dockerfile:1

ARG TARGETARCH
ARG VERSION

FROM docker.io/library/python:3.14-slim@sha256:e11c17f46f87983eb946b121694dedba6290ee2b0dd5294d137d82943502d179

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=65534:65534 bridge /app/bridge

USER 65534:65534

ENTRYPOINT ["python", "-m", "bridge.main"]
