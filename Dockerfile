# syntax=docker/dockerfile:1

ARG TARGETARCH
ARG VERSION

FROM docker.io/library/python:3.14-slim@sha256:b0c4ec81396588a94b99052caf2f786e6e92e03111991d3d40c68762ee48d2ab

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=65534:65534 bridge /app/bridge

USER 65534:65534

ENTRYPOINT ["python", "-m", "bridge.main"]
