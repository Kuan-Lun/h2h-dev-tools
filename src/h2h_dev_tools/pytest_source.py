"""Run repository pytest and local workers with fresh source import caches."""

from __future__ import annotations

import os
import runpy
import sys
import tempfile
from pathlib import Path


def main() -> None:
    """Preserve pytest arguments while isolating caches for this invocation.

    Call from a source-bootstrapped package before importing pytest or xdist.
    Local xdist workers inherit the cache settings and the verified package's
    import root; test selection, process ownership and deadlines stay local.
    """
    package = Path(__file__).resolve().parent
    parts = __package__.split(".")
    if tuple(package.parts[-len(parts) :]) != tuple(parts):
        raise RuntimeError("Pytest source package path does not match its namespace")
    import_root = str(package.parents[len(parts) - 1])
    environment_keys = (
        "PYTHONPYCACHEPREFIX",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
    )
    previous_environment = {key: os.environ.get(key) for key in environment_keys}
    previous_prefix = sys.pycache_prefix
    previous_write_flag = sys.dont_write_bytecode
    previous_path = sys.path
    with tempfile.TemporaryDirectory(prefix="h2h-pytest-source-") as cache:
        try:
            sys.pycache_prefix = cache
            sys.dont_write_bytecode = True
            sys.path = [import_root, *previous_path]
            os.environ["PYTHONPYCACHEPREFIX"] = cache
            os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
            previous_pythonpath = previous_environment["PYTHONPATH"]
            os.environ["PYTHONPATH"] = import_root + (
                os.pathsep + previous_pythonpath
                if previous_pythonpath is not None
                else ""
            )
            runpy.run_module("pytest", run_name="__main__", alter_sys=True)
        finally:
            sys.pycache_prefix = previous_prefix
            sys.dont_write_bytecode = previous_write_flag
            sys.path = previous_path
            for key, previous in previous_environment.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous
