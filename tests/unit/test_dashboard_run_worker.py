import json
from pathlib import Path

import pytest

from balance_pricing_engine.dashboard import run_worker
from balance_pricing_engine.pricing_intelligence.config import CollectionConfig
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService

ROOT = Path(__file__).resolve().parents[2]
BANKS = {"ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"}


@pytest.fixture
def run_dry(tmp_path, fixture_fetcher, monkeypatch):
    """Run the worker against saved bank pages (no network), returning (code, payload, db)."""

    def go(failing_banks=()):
        def service_factory(**kwargs):
            config = kwargs["config"].model_copy(
                update={"collection": CollectionConfig(inter_request_delay_seconds=0)}
            )
            return PricingIntelligenceService(
                config=config,
                store=kwargs["store"],
                fetcher=fixture_fetcher(failing_banks=failing_banks),
            )

        monkeypatch.setattr(run_worker, "PricingIntelligenceService", service_factory)
        database, result = tmp_path / "never.db", tmp_path / "result.json"
        code = run_worker.main(
            [
                "--config", str(ROOT / "configs/base.yaml"),
                "--database", str(database),
                "--result-file", str(result),
            ]
        )  # fmt: skip
        return code, json.loads(result.read_text()), database

    return go


def test_dry_run_collects_everything_and_persists_nothing(run_dry):
    code, payload, database = run_dry()
    assert code == 0 and payload["status"] == "SUCCESS"
    assert set(payload["summary"]["banks_succeeded"]) == BANKS
    assert payload["summary"]["rates_accepted"] > 0
    assert not database.exists()  # a dry run must not even create the store


def test_dry_run_reports_a_partial_failure_with_the_failed_bank(run_dry):
    code, payload, database = run_dry(failing_banks=["ANZ"])
    assert code == 2 and payload["status"] == "PARTIAL_SUCCESS"
    assert payload["summary"]["banks_failed"] == ["ANZ"]
    assert not database.exists()
