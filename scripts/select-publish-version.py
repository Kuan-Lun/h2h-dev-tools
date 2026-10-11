#!/usr/bin/env python3
"""Select an explicit release or a normal push version transition for CI."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def main() -> None:
    document = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    current = str(document["project"]["version"])
    event = os.environ["EVENT_NAME"]
    if event == "workflow_dispatch":
        requested = os.environ["REQUESTED_VERSION"]
        if requested != current:
            raise ValueError(
                f"requested version {requested!r} does not match {current}"
            )
        publish = True
    elif event == "push":
        with tempfile.TemporaryDirectory(prefix="h2h-publish-version-") as directory:
            output = Path(directory) / "outputs"
            subprocess.run(
                (
                    sys.executable,
                    "scripts/check-publish-version.py",
                    "--before-revision",
                    os.environ["BEFORE_SHA"],
                    "--output",
                    str(output),
                ),
                check=True,
            )
            fields = dict(
                line.split("=", 1) for line in output.read_text().splitlines()
            )
            if fields.get("bumped") not in {"true", "false"}:
                raise ValueError("invalid version checker result")
            publish = fields["bumped"] == "true"
    else:
        raise ValueError(f"unsupported publish event: {event}")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output_stream:
        print(f"publish={'true' if publish else 'false'}", file=output_stream)
    print(f"Release selection: version={current}, publish={publish}")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"select-publish-version: {error}", file=sys.stderr)
        raise SystemExit(1) from error
