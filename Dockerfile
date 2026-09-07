FROM node:22-alpine AS dashboard
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy \
    HORTATOR_DATA_DIR=/var/lib/hortator HORTATOR_WEB_DIR=/app/web/dist \
    TIKTOKEN_CACHE_DIR=/app/tiktoken_cache
RUN pip install --no-cache-dir uv==0.10.5
COPY pyproject.toml uv.lock ./
COPY hortator/ ./hortator/
RUN uv sync --frozen --no-dev --python /usr/local/bin/python \
    && .venv/bin/python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && useradd --system --uid 10001 --create-home hortator \
    && mkdir -p /var/lib/hortator \
    && chown -R hortator:hortator /var/lib/hortator /app/tiktoken_cache
COPY --from=dashboard /web/dist /app/web/dist
USER hortator
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]
CMD ["/app/.venv/bin/hortator", "serve", "--host", "0.0.0.0", "--port", "8000"]
