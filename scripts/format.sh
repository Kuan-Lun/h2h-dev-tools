#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
.venv/bin/ruff check --no-cache --fix .
.venv/bin/ruff format --no-cache .
node_modules/.bin/markdownlint-cli2 --fix
