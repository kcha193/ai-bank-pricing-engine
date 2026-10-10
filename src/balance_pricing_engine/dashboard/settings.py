"""Dashboard settings: which engine config, which observation database."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

import yaml

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.pricing_intelligence.config import (
    PricingIntelligenceConfig,
    resolve_database_path_from_env,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_ENV_VAR = "PRICING_WATCH_CONFIG"


@dataclass(frozen=True)
class DashboardSettings:
    intelligence: PricingIntelligenceConfig
    engine_config_path: Path
    engine_config_text: str
    backup_dir: Path
    repo_root: Path


def resolve_intelligence_config(config_yaml: str) -> PricingIntelligenceConfig:
    """``pricing_intelligence`` settings from an EngineConfig YAML, else defaults.

    An explicit ``database_path`` in the YAML wins; otherwise the
    ``PRICING_INTELLIGENCE_DB_PATH`` override applies exactly as in the CLI and the
    scheduled workflow, so all three read the same database.
    """
    env_default = resolve_database_path_from_env(PricingIntelligenceConfig())
    try:
        engine_config = EngineConfig(**yaml.safe_load(config_yaml))
    except Exception:  # noqa: BLE001 - any bad YAML falls back to defaults
        return env_default
    configured = engine_config.pricing_intelligence
    if configured is None:
        return env_default
    if "database_path" in configured.model_fields_set:
        return configured
    return configured.model_copy(update={"database_path": env_default.database_path})


def load_settings(
    env: Optional[Mapping[str, str]] = None, *, database_path: Optional[str] = None
) -> DashboardSettings:
    env = os.environ if env is None else env
    path = Path(env.get(CONFIG_ENV_VAR) or REPO_ROOT / "configs" / "base.yaml")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    intelligence = resolve_intelligence_config(text)
    if database_path is not None:
        intelligence = intelligence.model_copy(update={"database_path": database_path})
    return DashboardSettings(
        intelligence=intelligence,
        engine_config_path=path,
        engine_config_text=text,
        backup_dir=REPO_ROOT / "data" / "processed" / "pricing_intelligence" / "backups",
        repo_root=REPO_ROOT,
    )
