#!/usr/bin/env bash
set -euo pipefail
repository_root="$(git rev-parse --show-toplevel && printf '.')"
repository_root="${repository_root%.}"
repository_root="${repository_root%$'\n'}"
cd -- "$repository_root"
.venv/bin/ruff check --no-cache --fix .
.venv/bin/ruff format --no-cache .
node_modules/.bin/markdownlint-cli2 --fix
