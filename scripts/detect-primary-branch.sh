#!/usr/bin/env bash
set -euo pipefail
CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
repository_root=$PWD
exec bash "$repository_root/src/h2h_dev_tools/primary-branch.sh" "$repository_root" "$@"
