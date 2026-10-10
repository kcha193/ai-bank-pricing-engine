from pathlib import Path

import yaml

from balance_pricing_engine.dashboard.settings import load_settings, resolve_intelligence_config
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig

ROOT = Path(__file__).resolve().parents[2]


def _yaml(**intel):
    base = yaml.safe_load((ROOT / "configs/base.yaml").read_text())
    base["pricing_intelligence"] = intel
    return yaml.safe_dump(base)


def test_resolve_pulls_settings_from_the_yaml():
    cfg = resolve_intelligence_config(_yaml(selected_banks=["ANZ", "ASB"], database_path="x.db"))
    assert cfg.selected_banks == ["ANZ", "ASB"] and cfg.database_path == "x.db"


def test_resolve_falls_back_to_defaults_on_invalid_yaml(monkeypatch):
    monkeypatch.delenv("PRICING_INTELLIGENCE_DB_PATH", raising=False)
    assert resolve_intelligence_config("random_seed: [") == PricingIntelligenceConfig()


def test_resolve_falls_back_to_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PRICING_INTELLIGENCE_DB_PATH", str(tmp_path / "env.db"))
    assert resolve_intelligence_config("random_seed: [").database_path == str(tmp_path / "env.db")


def test_resolve_yaml_database_path_wins_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PRICING_INTELLIGENCE_DB_PATH", str(tmp_path / "env.db"))
    assert resolve_intelligence_config(_yaml(database_path="yaml.db")).database_path == "yaml.db"


def test_load_settings_defaults_to_base_yaml_and_overrides_database(tmp_path):
    settings = load_settings({}, database_path=str(tmp_path / "t.db"))
    assert settings.engine_config_path == ROOT / "configs" / "base.yaml"
    assert settings.intelligence.database_path == str(tmp_path / "t.db")
    assert settings.backup_dir == ROOT / "data" / "processed" / "pricing_intelligence" / "backups"
