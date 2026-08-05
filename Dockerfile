# syntax=docker/dockerfile:1

# Small Python base — ~50 MB compressed, more than enough for a FastAPI app.
# Pinned to a minor line rather than :latest so future rebuilds stay
# reproducible; bump the tag when you want a newer runtime.
FROM python:3.12-slim AS base

# Runtime env hygiene: don't buffer stdout, don't write .pyc files,
# don't let pip try to update itself mid-build, don't cache wheels.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user to run the app. Every container-hardening guide will
# tell you not to run as root and they're right.
RUN groupadd --system --gid 1000 autolox \
 && useradd  --system --uid 1000 --gid autolox --home /app autolox

WORKDIR /app

# Install dependencies first (cache layer) then app code — so a code
# change doesn't invalidate the pip layer.
COPY pyproject.toml README.md ./
COPY autolox/ ./autolox/
COPY autolox_web/ ./autolox_web/
COPY loxone_bulk_enroll.py ./

# `[web]` extra pulls fastapi/uvicorn/etc. Python 3.12 doesn't need the
# legacy-cgi shim (that's a 3.13+/3.14 problem).
# `--root-user-action=ignore` silences pip's generic "you're root" warning
# — expected and harmless during an image build; the runtime user is
# switched to `autolox` below.
RUN pip install --no-cache-dir --root-user-action=ignore -e '.[web]'

# The SQLite DB lives here — bind-mount or named-volume this in
# compose so it survives container recreation.
RUN mkdir -p /data && chown autolox:autolox /data
ENV AUTOLOX_DB=/data/autolox.db

USER autolox
EXPOSE 8000

# Simple healthcheck — hit the root page, which serves the SPA HTML.
# If uvicorn is dead this will fail; if uvicorn is up but Loxone is
# unreachable, this still passes (the connect-error state is a
# frontend concern, not a service-health concern).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "autolox_web.app:app", "--host", "0.0.0.0", "--port", "8000"]
