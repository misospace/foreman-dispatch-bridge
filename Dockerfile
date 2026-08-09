# syntax=docker/dockerfile:1

FROM docker.io/library/python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip "setuptools>=78.1.1" \
    && python -m pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=65534:65534 bridge /app/bridge

USER 65534:65534

# HEALTHCHECK validates that the bridge module can be imported and initialized.
# This catches deployment issues (e.g., missing dependencies, broken imports) early.
# Note: Kubernetes ignores Docker HEALTHCHECK for Deployment liveness/readiness probes;
# actual probe definitions in the manifest would still be needed if the bridge runs as a Deployment.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import bridge; print('ok')"

ENTRYPOINT ["python", "-m", "bridge.main"]
