#!/usr/bin/env python3
"""Review this repository with its authoritative shared implementation."""

import sys
from pathlib import Path


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repository / "src"))
    from h2h_dev_tools.code_review import main as review

    review(repository)


if __name__ == "__main__":
    main()
