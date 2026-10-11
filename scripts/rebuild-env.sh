#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
uv venv --clear --python 3.14 .venv
uv pip install --python .venv/bin/python --refresh --editable ".[dev]"
npm install --package-lock=false --ignore-scripts --no-audit --no-fund
