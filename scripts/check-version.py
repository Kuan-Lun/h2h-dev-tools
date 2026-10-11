#!/usr/bin/env python3
"""Run the shared version policy implementation."""

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    runpy.run_module("h2h_dev_tools.version_policy", run_name="__main__")
