#!/usr/bin/env python3
"""Generate src/ophix_task_systemd/_version.py from pyproject.toml."""

import tomli
from pathlib import Path

pyproject_path = Path(__file__).parent / "pyproject.toml"
version_file_path = Path(__file__).parent / "src" / "ophix_task_systemd" / "_version.py"

with pyproject_path.open("rb") as f:
    pyproject_data = tomli.load(f)

version = pyproject_data["project"]["version"]
version_file_path.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
print(f"Generated {version_file_path} with version {version}")
