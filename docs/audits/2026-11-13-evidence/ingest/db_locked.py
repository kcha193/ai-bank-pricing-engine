"""A concurrent writer (e.g. the other product's scheduled run or a backup) holding the SQLite write lock >5 s."""
import importlib.util, sqlite3, tempfile, json, time
from pathlib import Path
from harness import *  # noqa
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle

svc, db = make_service()
run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=T0, alert_sinks={})
blocker = sqlite3.connect(db, timeout=0)
blocker.execute("BEGIN EXCLUSIVE")
t = time.time()
try:
    r = run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=T0 + timedelta(days=1), alert_sinks={})
    print("completed", r.code)
except Exception as exc:  # what the scheduled script would see
    print(f"raised {type(exc).__name__}: {exc} after {time.time()-t:.1f}s -- run_collection() only catches ChronologicalOrderError,"
          " so the workflow would exit via an uncaught traceback (Python exit 1) without writing --summary-file or a RunRecord")
blocker.rollback()
con = sqlite3.connect(db)
print("operational_runs after:", con.execute("select status from operational_runs").fetchall())
print("connect() timeout used by ObservationStore: default (5.0 s); journal_mode:", con.execute("pragma journal_mode").fetchone()[0])
