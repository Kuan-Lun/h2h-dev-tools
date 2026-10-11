#!/usr/bin/env python3
"""Run the shared version policy implementation."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    package = Path(__file__).resolve().parents[1] / "src/h2h_dev_tools"
    runpy.run_path(str(package / "source_loader.py"))["load_package"](
        package, "h2h_dev_tools"
    )
    runpy.run_module("h2h_dev_tools.version_policy", run_name="__main__")
