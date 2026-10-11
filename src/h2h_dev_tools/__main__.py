"""Explicit source synchronization and offline verification commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .bundle import sync_bundle
from .bundle_verify import BundleError, verify_bundle
from .migrate_v1 import migrate_v1


def main(arguments: list[str] | None = None) -> int:
    """Execute one toolkit operation against an explicitly selected repository."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("sync", "verify", "migrate-v1"):
        command = commands.add_parser(name)
        command.add_argument("--repository", required=True, type=Path)
    options = parser.parse_args(arguments)
    try:
        if options.command in {"sync", "migrate-v1"}:
            operation = sync_bundle if options.command == "sync" else migrate_v1
            changed = operation(options.repository)
            print(
                "Source bundle synchronized."
                if changed
                else "Source bundle is unchanged."
            )
        else:
            manifest = verify_bundle(options.repository)
            print(
                f"Verified h2h-dev-tools {manifest['toolkit_version']} source bundle."
            )
    except BundleError as error:
        print(f"h2h-dev-tools: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
