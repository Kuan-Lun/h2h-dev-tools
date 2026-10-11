#!/usr/bin/env python3
"""Install declared CI dependencies from this repository, regardless of cwd."""

import runpy
from pathlib import Path

repository = Path(__file__).resolve().parents[1]
package = repository / "src/h2h_dev_tools"
runpy.run_path(str(package / "source_loader.py"))["load_package"](
    package, "h2h_dev_tools"
)
from h2h_dev_tools.ci_dependencies import main  # noqa: E402 — source checkout bootstrap

if __name__ == "__main__":
    main(repository=repository)
