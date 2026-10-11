#!/usr/bin/env bash
set -euo pipefail
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$repository_root/src/h2h_dev_tools/primary-branch.sh" "$repository_root" "$@"
