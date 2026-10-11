#!/usr/bin/env python3
"""Review this repository with its authoritative shared implementation."""

import runpy
from pathlib import Path


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    package = repository / "src/h2h_dev_tools"
    runpy.run_path(str(package / "source_loader.py"))["load_package"](
        package, "h2h_dev_tools"
    )
    from h2h_dev_tools.code_review import main as review

    review(repository)


if __name__ == "__main__":
    main()
