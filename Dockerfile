FROM node:22-alpine AS dashboard
ARG HORTATOR_BUILD_INFO
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.14-slim-trixie
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy \
    HORTATOR_DATA_DIR=/var/lib/hortator HORTATOR_WEB_DIR=/app/web/dist \
    TIKTOKEN_CACHE_DIR=/app/tiktoken_cache
RUN apt-get update && apt-get install -y --no-install-recommends \
    bubblewrap libseccomp2 ca-certificates bash coreutils findutils grep sed gawk \
    curl wget git jq perl procps iproute2 net-tools file gzip tar zip unzip \
    diffutils patch xz-utils bzip2 binutils \
    && rm -rf /var/lib/apt/lists/* \
    && python3.14 -m pip install --no-cache-dir uv==0.12.9
COPY scripts/install-micromamba.py /tmp/install-micromamba.py
RUN python3.14 /tmp/install-micromamba.py --bin-dir /usr/local/bin \
    && rm /tmp/install-micromamba.py
COPY pyproject.toml uv.lock .python-version ./
COPY hortator/ ./hortator/
RUN uv sync --frozen --no-dev --python /usr/local/bin/python3.14 \
    && .venv/bin/python3.14 -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && useradd --system --uid 10001 --create-home hortator \
    && mkdir -p /var/lib/hortator \
    && chown -R hortator:hortator /var/lib/hortator /app/tiktoken_cache
COPY --from=dashboard /web/dist /app/web/dist
COPY --from=dashboard /web/dist/build-info.json /app/hortator/_build_info.json
USER hortator
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD ["/app/.venv/bin/python3.14", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]
CMD ["/app/.venv/bin/hortator", "serve", "--host", "0.0.0.0", "--port", "8000"]
