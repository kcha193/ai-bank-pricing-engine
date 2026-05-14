"""Config snapshot writer for the pricing run ledger."""

from __future__ import annotations

import shutil
from pathlib import Path


def write_config_snapshot(config_path: str | Path, run_dir: Path) -> None:
    """Copy the config YAML into run_dir as config_snapshot.yaml."""
    shutil.copy2(config_path, run_dir / "config_snapshot.yaml")
