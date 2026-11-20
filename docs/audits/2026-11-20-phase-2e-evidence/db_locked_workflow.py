"""DATA-009 replay through the scheduled workflow's entry point (Phase 2e).

Same scenario as ../2026-11-13-evidence/ingest/db_locked.py (left unmodified): a concurrent
writer -- e.g. the other product's run or a backup -- holds the SQLite write lock longer
than the busy timeout during a term-deposit collection. Here the collection goes through
scripts/collect_market_rates_workflow.run_collection(), the function the scheduler runs.

Run from the repository root:  python docs/audits/2026-11-20-phase-2e-evidence/db_locked_workflow.py
"""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path("docs/audits/2026-11-13-evidence/ingest").resolve()))
from harness import T0, make_service  # noqa: E402

from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle  # noqa: E402

spec = importlib.util.spec_from_file_location("wf", "scripts/collect_market_rates_workflow.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)

svc, db = make_service()
run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=T0, alert_sinks={})
config = svc.config.model_copy(update={"sqlite_busy_timeout_seconds": 2.0})
fetcher_service = svc


def blocking_collectors(**kwargs):
    # The harness's fixture collectors, plus a concurrent writer that grabs the lock
    # after the first bank is fetched (mid-run, before the store write).
    collectors = list(fetcher_service.collectors)
    original = collectors[0].collect_term_deposit_rates

    def grab_then_collect(*args, **kw):
        blocker = sqlite3.connect(db, timeout=0, check_same_thread=False)
        blocker.execute("BEGIN EXCLUSIVE")
        holder.append(blocker)
        return original(*args, **kw)

    collectors[0].collect_term_deposit_rates = grab_then_collect
    return collectors


holder = []
workflow_service_module = sys.modules["balance_pricing_engine.pricing_intelligence.service"]
workflow_service_module.build_collectors = blocking_collectors
summary_path = Path(tempfile.mkdtemp()) / "last_run_term_deposit.json"
started = time.time()
try:
    code, summary = workflow.run_collection(
        config, render_javascript=False, summary_path=summary_path,
        product_type="term_deposit", run_id="db-locked-replay",
    )
finally:
    for blocker in holder:
        blocker.rollback()
        blocker.close()
print(f"exit code: {code} after {time.time() - started:.1f}s (busy timeout 2.0 s)")
written = json.loads(summary_path.read_text())
print("summary file written:", summary_path.exists())
for key in ("failure_type", "error_type", "run_id", "product_type", "run_record_persisted",
            "run_record_note", "banks_failed"):
    print(f"  {key}: {written[key]!r}")
con = sqlite3.connect(db)
print("operational_runs after:", con.execute("select run_id, status from operational_runs").fetchall())
print("observations rows unchanged by the locked run:",
      con.execute("select count(*) from collection_runs").fetchone()[0] == 1)
print("journal_mode:", con.execute("pragma journal_mode").fetchone()[0])
