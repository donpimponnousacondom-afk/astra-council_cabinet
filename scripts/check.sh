#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen --python 3.14
uv run ruff check hortator tests
uv run ruff format --check --quiet hortator tests scripts
uv run pytest -q
npm ci --prefix web
npm run format:check --prefix web
npm run build --prefix web
npm test --prefix web
