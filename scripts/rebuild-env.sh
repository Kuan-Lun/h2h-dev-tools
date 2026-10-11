#!/usr/bin/env bash
set -euo pipefail
repository_root="$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && printf '%s.' "$PWD")"
repository_root=${repository_root%.}
exec bash "$repository_root/src/h2h_dev_tools/rebuild-env.sh" "$repository_root" "" "$@"
