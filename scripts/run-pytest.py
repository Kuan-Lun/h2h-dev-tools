#!/usr/bin/env python3
"""Run pytest against checkout source, ignoring toolkit bytecode caches."""

import runpy
from pathlib import Path


def main() -> None:
    package = Path(__file__).resolve().parents[1] / "src/h2h_dev_tools"
    runpy.run_path(str(package / "source_loader.py"))["load_package"](
        package, "h2h_dev_tools"
    )
    from h2h_dev_tools.pytest_source import main as run_pytest

    run_pytest()


if __name__ == "__main__":
    main()
