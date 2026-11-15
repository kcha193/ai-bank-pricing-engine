"""Config artifacts for the pricing run ledger."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from balance_pricing_engine.config_identity import canonical_config_bytes

if TYPE_CHECKING:
    from balance_pricing_engine.config import EngineConfig

EFFECTIVE_CONFIG_FILENAME = "effective_config.json"


def write_config_snapshot(config_path: str | Path, run_dir: Path) -> None:
    """Copy the config YAML into run_dir as config_snapshot.yaml."""
    shutil.copy2(config_path, run_dir / "config_snapshot.yaml")


def write_effective_config(config: "EngineConfig", run_dir: Path) -> Path:
    """Write the validated config that was priced as effective_config.json.

    The file holds exactly the canonical bytes that ``config_hash`` is computed over,
    so ``sha256(effective_config.json)`` equals ``metadata.config_hash`` and
    ``decision_record.config_hash``.
    """
    path = run_dir / EFFECTIVE_CONFIG_FILENAME
    path.write_bytes(canonical_config_bytes(config))
    return path
