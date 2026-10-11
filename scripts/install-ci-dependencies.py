#!/usr/bin/env python3
"""Install declared CI dependencies from this repository, regardless of cwd."""

import sys
from pathlib import Path

repository = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repository / "src"))
from h2h_dev_tools.ci_dependencies import main  # noqa: E402 — source checkout bootstrap

if __name__ == "__main__":
    main(repository=repository)
