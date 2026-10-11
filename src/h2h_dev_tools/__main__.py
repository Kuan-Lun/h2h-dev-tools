"""Explicit source synchronization and offline verification commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .bundle import sync_bundle
from .bundle_verify import BundleError, verify_bundle


def main(arguments: list[str] | None = None) -> int:
    """Execute one toolkit operation against an explicitly selected repository."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("sync", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--repository", required=True, type=Path)
    options = parser.parse_args(arguments)
    try:
        if options.command == "sync":
            changed = sync_bundle(options.repository)
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
