# OpenChart server (SPEC section 15). Data in the /data volume; listens on port 8000 inside
# the container (publish it with docker-compose.yml, which picks the host address).
# Secrets: there is no credential store in a container, so the tokens saved in the web page
# go to /data/secrets.enc, encrypted with OPENCHART_SECRET_KEY (or the file
# OPENCHART_SECRET_KEY_FILE names, for Docker secrets). Create a key with:
#   docker run --rm openchart:local openchart secrets new-key

# --- The web UI ---------------------------------------------------------------------------
FROM node:24-slim AS web
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Vite writes to ../src/openchart/web
RUN npm run build

# --- The server ---------------------------------------------------------------------------
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENCHART_DATA_DIR=/data \
    OPENCHART_HOST=0.0.0.0 \
    OPENCHART_PORT=8000

WORKDIR /build
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
COPY --from=web /build/src/openchart/web ./src/openchart/web
RUN pip install . \
    && mkdir -p /usr/share/doc/openchart \
    && cp LICENSE NOTICE /usr/share/doc/openchart/ \
    && cd / && rm -rf /build \
    && useradd --system --uid 10001 --no-create-home --home-dir /data openchart \
    && mkdir /data && chown openchart:openchart /data && chmod 700 /data

# /data is the server's only (mode 700): the indicator sidecar (uid 10002) cannot even
# list the image's empty folder, let alone a volume (docs/custom-indicators.md).
USER openchart
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('OPENCHART_PORT', '8000'), timeout=4)"]
CMD ["openchart", "run"]
