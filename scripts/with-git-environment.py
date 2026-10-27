#!/usr/bin/env python3
"""Run a test child through this checkout's authoritative Git environment helper."""

import runpy
from pathlib import Path


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    package = repository / "src/h2h_dev_tools"
    runpy.run_path(str(package / "source_loader.py"))["load_package"](
        package, "h2h_dev_tools"
    )
    from h2h_dev_tools.git_environment import main as run

    raise SystemExit(run())


if __name__ == "__main__":
    main()
