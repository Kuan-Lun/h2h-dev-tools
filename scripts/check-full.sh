#!/usr/bin/env bash
set -euo pipefail
repository_root="$(git rev-parse --show-toplevel && printf '.')"
repository_root="${repository_root%.}"
repository_root="${repository_root%$'\n'}"
CDPATH= cd -- "$repository_root"
scripts/check-fast.sh
.venv/bin/python -B src/h2h_dev_tools/git_environment.py --repository "$PWD" -- \
    .venv/bin/python -B scripts/run-pytest.py -p no:cacheprovider
.venv/bin/python -B scripts/build-and-verify-distributions.py
