#!/usr/bin/env python3
"""Run the repository tool directly from its authoritative source."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("h2h_dev_tools.change_scope", run_name="__main__")
