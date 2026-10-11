#!/usr/bin/env python3
"""Run pytest against checkout source, ignoring toolkit bytecode caches."""

import runpy
from pathlib import Path

package = Path(__file__).resolve().parents[1] / "src/h2h_dev_tools"
runpy.run_path(str(package / "source_loader.py"))["load_package"](
    package, "h2h_dev_tools"
)
runpy.run_module("pytest", run_name="__main__")
