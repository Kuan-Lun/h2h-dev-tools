#!/usr/bin/env python3
"""Run the repository tool directly from its authoritative source."""

import runpy
from pathlib import Path

package = Path(__file__).resolve().parents[1] / "src/h2h_dev_tools"
runpy.run_path(str(package / "source_loader.py"))["load_package"](
    package, "h2h_dev_tools"
)
runpy.run_module("h2h_dev_tools.change_scope", run_name="__main__")
