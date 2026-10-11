#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
scripts/check-fast.sh
.venv/bin/python -B src/h2h_dev_tools/git_environment.py --repository "$PWD" -- \
    .venv/bin/python -B -m pytest -p no:cacheprovider
.venv/bin/python -B scripts/build-and-verify-distributions.py
