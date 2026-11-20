"""``pricing_intelligence collect`` concurrency and run-ledger behaviour (DATA-010).

``collect`` used to call ``service.collect()`` with no single-instance lock and no
operational RunRecord, so it could interleave with ``run`` or the scheduled workflow
against the same database. It now shares their product-scoped lock and the
``run_operational_cycle`` classification, while staying collection-only: no
scenario/optimiser work and no real alert delivery.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3

import pytest

from balance_pricing_engine.competitor_rates.collectors import (
    BankCollectionResult,
    CollectionStatus,
)
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence import cli
from balance_pricing_engine.pricing_intelligence import alerts as alerts_module
from balance_pricing_engine.pricing_intelligence.locking import SingleInstanceLock
from balance_pricing_engine.pricing_intelligence.orchestration import (
    EXIT_ALREADY_RUNNING,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_TOTAL_FAILURE,
    default_lock_file_path,
)
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

NOW = datetime.now(timezone.utc) - timedelta(hours=1)
BANKS = ("ANZ", "ASB", "BNZ")


class StubCollector:
    def __init__(self, bank, record, calls, fail=False):
        self.bank = bank
        self.record = record
        self.calls = calls
        self.fail = fail

    def _result(self, product):
        self.calls.append((self.bank, product))
        if self.fail:
            return BankCollectionResult(
                bank=self.bank,
                product_type=product,
                status=CollectionStatus.FAILED,
                source_url="https://www.example-bank.co.nz/rates",
                error="simulated readiness timeout",
                error_type="ReadinessTimeoutError",
            )
        return BankCollectionResult(
            bank=self.bank,
            product_type=product,
            status=CollectionStatus.SUCCESS,
            source_url="https://www.example-bank.co.nz/rates",
            records=(self.record.model_copy(update={"bank": self.bank, "captured_at_utc": NOW}),),
        )

    def collect_mortgage_rates(self, captured_at_utc=None, persist_snapshot=True):
        return self._result(ProductType.HOME_LOAN)

    def collect_term_deposit_rates(self, captured_at_utc=None, persist_snapshot=True):
        return self._result(ProductType.TERM_DEPOSIT)


@pytest.fixture
def stub_banks(monkeypatch, make_record):
    """Install stub collectors; returns the list of (bank, product) fetches made."""
    calls: list = []

    def install(failing=(), product=ProductType.HOME_LOAN):
        record = (
            make_record(product_type="term_deposit", rate_category="term_deposit")
            if product is ProductType.TERM_DEPOSIT
            else make_record()
        )
        monkeypatch.setattr(
            "balance_pricing_engine.pricing_intelligence.service.build_collectors",
            lambda **kwargs: [
                StubCollector(bank, record, calls, fail=bank in failing) for bank in BANKS
            ],
        )
        return calls

    return install


@pytest.fixture(autouse=True)
def no_real_alerts(monkeypatch):
    monkeypatch.setenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", "https://hooks.example.test/x")

    def forbidden(*args, **kwargs):  # pragma: no cover - asserting it is never reached
        raise AssertionError("collect must not deliver real alerts")

    monkeypatch.setattr(alerts_module, "urlopen", forbidden)


def _runs(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT status, product_type FROM operational_runs ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()


def test_t30_t31_t32_collect_takes_the_shared_lock_and_skips_without_fetching(
    tmp_path, stub_banks, capsys
):
    calls = stub_banks()
    database = tmp_path / "observations.db"
    lock = default_lock_file_path(ProductType.HOME_LOAN, database)
    with SingleInstanceLock(Path(lock)):
        code = cli.main(["--database", str(database), "collect", "--no-snapshot"])
    assert code == EXIT_ALREADY_RUNNING
    assert calls == []
    assert "Skipped" in capsys.readouterr().out
    assert not database.exists(), "no store may be opened before the lock is held"


def test_collect_honours_an_explicit_lock_file(tmp_path, stub_banks):
    calls = stub_banks()
    lock = tmp_path / "custom.lock"
    with SingleInstanceLock(lock):
        code = cli.main(
            ["--database", str(tmp_path / "o.db"), "collect", "--lock-file", str(lock)]
        )
    assert code == EXIT_ALREADY_RUNNING
    assert calls == []


def test_collect_and_run_share_one_lock(tmp_path, stub_banks):
    """T36: the lock `collect` takes is the one `run` (and the scheduler) use."""
    stub_banks()
    database = tmp_path / "observations.db"
    lock = Path(default_lock_file_path(ProductType.HOME_LOAN, database))
    with SingleInstanceLock(lock):
        assert cli.main(["--database", str(database), "run"]) == EXIT_ALREADY_RUNNING
        assert cli.main(["--database", str(database), "collect"]) == EXIT_ALREADY_RUNNING


def test_t33_collect_persists_an_operational_run_record(tmp_path, stub_banks, capsys):
    stub_banks()
    database = tmp_path / "observations.db"
    code = cli.main(["--database", str(database), "collect", "--no-snapshot"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert _runs(database) == [("SUCCESS", "home_loan")]
    assert "Run id:" in out
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT count(*) FROM scenario_runs").fetchone()[0] == 0
    connection.close()


def test_t34_collect_reports_a_partial_failure(tmp_path, stub_banks, capsys):
    stub_banks(failing=("BNZ",))
    database = tmp_path / "observations.db"
    code = cli.main(["--database", str(database), "collect", "--no-snapshot"])
    out = capsys.readouterr().out
    assert code == EXIT_PARTIAL_FAILURE
    assert _runs(database) == [("PARTIAL_SUCCESS", "home_loan")]
    assert "PARTIAL_SUCCESS" in out


def test_collect_reports_a_total_failure(tmp_path, stub_banks):
    stub_banks(failing=BANKS)
    database = tmp_path / "observations.db"
    assert cli.main(["--database", str(database), "collect"]) == EXIT_TOTAL_FAILURE
    assert _runs(database) == [("FAILED", "home_loan")]


def test_collect_term_deposits_uses_its_own_lock_and_ledger(tmp_path, stub_banks):
    stub_banks(product=ProductType.TERM_DEPOSIT)
    database = tmp_path / "observations.db"
    home_lock = Path(default_lock_file_path(ProductType.HOME_LOAN, database))
    with SingleInstanceLock(home_lock):
        code = cli.main(
            ["--database", str(database), "--product", "term_deposit", "collect"]
        )
    assert code == EXIT_OK
    assert _runs(database) == [("SUCCESS", "term_deposit")]


def test_t35_no_persist_preview_is_non_mutating(tmp_path, stub_banks, capsys):
    calls = stub_banks()
    database = tmp_path / "observations.db"
    assert cli.main(["--database", str(database), "collect", "--no-snapshot"]) == EXIT_OK
    capsys.readouterr()
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    lock = Path(default_lock_file_path(ProductType.HOME_LOAN, database))
    lock.unlink(missing_ok=True)
    calls.clear()

    with SingleInstanceLock(tmp_path / "unrelated.lock"):
        code = cli.main(["--database", str(database), "collect", "--no-persist"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert calls, "a preview still collects"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    assert _runs(database) == [("SUCCESS", "home_loan")]
    assert not lock.exists(), "a preview takes no lock"
    assert "Preview" in out and "nothing persisted" in out


def test_no_persist_against_a_missing_database_creates_nothing(tmp_path, stub_banks, capsys):
    stub_banks()
    database = tmp_path / "missing" / "observations.db"
    code = cli.main(["--database", str(database), "collect", "--no-persist"])
    assert code == EXIT_OK
    assert not database.parent.exists()


def test_no_persist_runs_even_while_a_real_collection_holds_the_lock(tmp_path, stub_banks):
    stub_banks()
    database = tmp_path / "observations.db"
    lock = Path(default_lock_file_path(ProductType.HOME_LOAN, database))
    with SingleInstanceLock(lock):
        assert cli.main(["--database", str(database), "collect", "--no-persist"]) == EXIT_OK


def test_a_locked_database_is_reported_as_database_locked(tmp_path, stub_banks, capsys):
    stub_banks()
    database = tmp_path / "observations.db"
    ObservationStore(database).close()
    blocker = sqlite3.connect(database, timeout=0)
    blocker.execute("BEGIN IMMEDIATE")
    yaml_path = tmp_path / "config.yaml"
    import yaml

    base = yaml.safe_load(Path("configs/base.yaml").read_text())
    base["pricing_intelligence"] = {"sqlite_busy_timeout_seconds": 0.2}
    yaml_path.write_text(yaml.safe_dump(base))
    try:
        code = cli.main(
            ["--config", str(yaml_path), "--database", str(database), "collect"]
        )
    finally:
        blocker.rollback()
        blocker.close()
    assert code == EXIT_TOTAL_FAILURE
    assert "database_locked" in capsys.readouterr().out
    assert _runs(database) == []


def test_no_persist_preview_never_writes_raw_html(tmp_path, monkeypatch):
    from balance_pricing_engine.competitor_rates.collectors import ANZCollector
    from balance_pricing_engine.competitor_rates.collection_config import CollectionConfig

    raw = tmp_path / "raw"
    collector = ANZCollector(
        config=CollectionConfig(),
        raw_root=raw,
        fetcher=lambda url: (
            b"<h1>Home loan rates</h1><table><tr><th>Term</th><th>Rate</th>"
            b"</tr><tr><td>1 year</td><td>4.89%</td></tr></table>"
        ),
    )
    monkeypatch.setattr(
        "balance_pricing_engine.pricing_intelligence.service.build_collectors",
        lambda **kwargs: [collector],
    )
    cli.main(["--database", str(tmp_path / "missing.db"), "collect", "--no-persist"])
    assert not raw.exists()
