#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
for executable in .venv/bin/ruff .venv/bin/mypy node_modules/.bin/markdownlint-cli2; do
    [[ -x "$executable" ]] || {
        printf 'Missing local tool: %s; run scripts/rebuild-env.sh\n' "$executable" >&2
        exit 1
    }
done
.venv/bin/ruff check --no-cache .
.venv/bin/ruff format --no-cache --check .
.venv/bin/mypy --config-file pyproject.toml --no-incremental --cache-dir=/dev/null
node_modules/.bin/markdownlint-cli2
