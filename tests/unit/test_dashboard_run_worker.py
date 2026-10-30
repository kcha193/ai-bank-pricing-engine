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

    def go(failing_banks=(), product=None):
        def service_factory(**kwargs):
            config = kwargs["config"].model_copy(
                update={"collection": CollectionConfig(inter_request_delay_seconds=0)}
            )
            return PricingIntelligenceService(
                config=config,
                store=kwargs["store"],
                fetcher=fixture_fetcher(failing_banks=failing_banks),
                raw_root=tmp_path / "raw",  # never the repository's data/raw/
            )

        monkeypatch.setattr(run_worker, "PricingIntelligenceService", service_factory)
        database, result = tmp_path / "never.db", tmp_path / "result.json"
        code = run_worker.main(
            [
                "--config", str(ROOT / "configs/base.yaml"),
                "--database", str(database),
                "--result-file", str(result),
                *(["--product", product] if product else []),
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


def test_dry_run_defaults_to_home_loans_for_existing_callers(run_dry):
    _, payload, _ = run_dry()
    assert payload["summary"]["product_type"] == "home_loan"


def test_term_deposit_dry_run_collects_every_bank_and_persists_nothing(run_dry):
    """Runs the real fetch -> parse -> validate path on the 2026-10-30 page structures."""
    code, payload, database = run_dry(product="term_deposit")
    assert code == 0 and payload["status"] == "SUCCESS", payload["summary"]["failures"]
    assert payload["summary"]["product_type"] == "term_deposit"
    assert set(payload["summary"]["banks_succeeded"]) == BANKS
    assert payload["summary"]["rates_accepted"] > 0
    assert payload["summary"]["validation_errors"] == 0
    assert not database.exists()


def test_term_deposit_dry_run_reports_a_partial_failure(run_dry):
    code, payload, _ = run_dry(failing_banks=["Kiwibank"], product="term_deposit")
    assert code == 2 and payload["status"] == "PARTIAL_SUCCESS"
    assert payload["summary"]["banks_failed"] == ["Kiwibank"]
