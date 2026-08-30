# syntax=docker/dockerfile:1

FROM docker.io/library/python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# TEMPORARY (remove me): CVE-2026-53615 is a HIGH in util-linux 2.41-5, which the
# pinned python:3.14-slim base still ships. Debian fixed it in 2.41.5-0+deb13u1,
# but the upstream image has not rebuilt against that snapshot, so the release
# Trivy gate fails on nine packages that all come from this one source. Pull the
# fixed packages straight from Debian until the base catches up.
#
# Drop this layer once the base ships the fix, i.e. when
#   docker run --rm <base-digest> dpkg-query -W -f='${Version}\n' util-linux
# reports 2.41.5-0+deb13u1 or newer. At that point it is a no-op costing build time.
# Versions are pinned so this layer is deterministic: a bare `apt-get install`
# would resolve against whatever the mirror holds at build time, which is the
# same reproducibility hole that argues against a blanket `apt-get upgrade`.
# If Debian supersedes these, the build fails loudly rather than drifting, which
# is the reminder to check whether the base has caught up and this can go.
#
# Same situation for CVE-2026-14456, a HIGH in openssl 3.5.6-1~deb13u2 (QUIC
# server unbounded memory growth). Debian fixed it in 3.5.7-1~deb13u2; the base
# still ships 3.5.6, verified against the current 3.14-slim digest, so bumping
# the pin does not help. Drop these three lines when
#   docker run --rm <base-digest> dpkg-query -W -f='${Version}\n' openssl
# reports 3.5.7-1~deb13u2 or newer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade \
        util-linux=2.41.5-0+deb13u1 \
        bsdutils=1:2.41.5-0+deb13u1 \
        mount=2.41.5-0+deb13u1 \
        login=1:4.16.0-2+really2.41.5-0+deb13u1 \
        openssl=3.5.7-1~deb13u2 \
        libssl3t64=3.5.7-1~deb13u2 \
        openssl-provider-legacy=3.5.7-1~deb13u2 \
    && rm -rf /var/lib/apt/lists/*

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
