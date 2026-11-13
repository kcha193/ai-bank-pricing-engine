"""Challenge case Q: duplicates, failed run, out-of-order and corrected observations against a temp SQLite store."""
import sqlite3
from harness import *  # noqa
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle
from balance_pricing_engine.pricing_intelligence.store import ChronologicalOrderError
from balance_pricing_engine.pricing_intelligence.strategy import resolve_competitor_rate

P = "term_deposit"
def asb_with(rate_12m: str, extra_row: str = ""):
    html = TD_FIXTURE["ASB"].read_text()
    return html.replace("<tr><td>12 Months</td><td>4.00% p.a.</td></tr>",
                        f"<tr><td>12 Months</td><td>{rate_12m}% p.a.</td></tr>{extra_row}")

def swap(svc, mapping):
    svc._collectors = build_collectors(banks=BANKS, config=svc.config.collection, fetcher=make_fetcher(mapping), raw_root=svc._raw_root)

def counts(db, label):
    con = sqlite3.connect(db)
    q = lambda s: con.execute(s).fetchone()[0]
    obs = q("select count(*) from observations where product_type='term_deposit'")
    crp = q("select count(*) from collection_run_products where product_type='term_deposit'")
    opr = q("select count(*) from operational_runs where product_type='term_deposit'")
    rc = q("select count(*) from rate_changes where product_type='term_deposit'")
    asb = con.execute("select rate_pct, validation_status, first_observed_at, last_seen_at, observation_count from observations "
                      "where bank='ASB' and product_type='term_deposit' and term_months=12 and rate_category='term_deposit' order by id").fetchall()
    print(f"  [{label}] observations={obs} collection_run_products={crp} operational_runs={opr} rate_changes={rc} ASB12 rows={asb}")

def asb12(svc, now):
    t = svc.get_market_snapshot(product_type=P, now=now).term(12)
    e = [x for x in (t.entries if t else ()) if x.bank == "ASB"]
    return (e[0].rate_pct, e[0].observed_at.isoformat(), e[0].quarantined) if e else None

svc, db = make_service()
print("Q1 baseline + idempotent re-run of the same run_id")
r1 = run_operational_cycle(svc, product_type=P, captured_at_utc=T0, run_id="run-A", alert_sinks={})
counts(db, "after run-A")
r1b = run_operational_cycle(svc, product_type=P, captured_at_utc=T0, run_id="run-A", alert_sinks={})
print("  retry same run_id: already_recorded=", r1b.outcome.already_recorded, "code", r1b.code, "changes", len(r1b.outcome.changes))
counts(db, "after run-A retry")

print("Q2 identical re-collection with a new run_id (T0+1d)")
r2 = run_operational_cycle(svc, product_type=P, captured_at_utc=T0 + timedelta(days=1), run_id="run-B", alert_sinks={})
print("  inserted", r2.outcome.write_result.inserted, "unchanged", r2.outcome.write_result.unchanged, "changes", len(r2.outcome.changes))
counts(db, "after run-B")

print("Q3 duplicate row on page (same term, same rate) and conflicting duplicate (same term, different rate)")
swap(svc, url_map({"ASB:term_deposit": asb_with("4.00", "<tr><td>12 Months</td><td>4.00% p.a.</td></tr>")}))
r3 = run_operational_cycle(svc, product_type=P, captured_at_utc=T0 + timedelta(days=2), run_id="run-C", alert_sinks={})
print("  exact dup: code", r3.code, "rejected", len(r3.outcome.validation.rejected), "changes", [c.describe() for c in r3.outcome.changes])
swap(svc, url_map({"ASB:term_deposit": asb_with("4.00", "<tr><td>12 Months</td><td>4.40% p.a.</td></tr>")}))
r3b = run_operational_cycle(svc, product_type=P, captured_at_utc=T0 + timedelta(days=3), run_id="run-D", alert_sinks={})
print("  conflict: code", r3b.code, "rejected", [(x.bank, x.term_label_raw, x.advertised_rate_pct) for x in r3b.outcome.validation.rejected],
      "changes", [c.describe() for c in r3b.outcome.changes], "ASB12 in snapshot", asb12(svc, T0 + timedelta(days=3, hours=1)))
counts(db, "after run-D")

print("Q4 failed run (total outage) then recovery with a genuine -10bps move")
swap(svc, url_map({f"{b}:{P}": None for b in BANKS}))
r4 = run_operational_cycle(svc, product_type=P, captured_at_utc=T0 + timedelta(days=4), run_id="run-E", alert_sinks={})
print("  outage: code", r4.code, "changes", len(r4.outcome.changes), "write_result", r4.outcome.write_result)
counts(db, "after failed run-E")
swap(svc, url_map({"ASB:term_deposit": asb_with("3.90")}))
r5 = run_operational_cycle(svc, product_type=P, captured_at_utc=T0 + timedelta(days=5), run_id="run-F", alert_sinks={})
print("  recovery: code", r5.code, "changes", [c.describe() for c in r5.outcome.changes])
counts(db, "after run-F")

print("Q5 out-of-order (backdated) collection")
try:
    run_operational_cycle(svc, product_type=P, captured_at_utc=T0 + timedelta(days=4, hours=12), run_id="run-late", alert_sinks={})
    print("  backdated run ACCEPTED (unexpected)")
except ChronologicalOrderError as exc:
    print("  backdated run rejected:", str(exc)[:110])
r_same = run_operational_cycle(svc, product_type=P, captured_at_utc=T0 + timedelta(days=5), run_id="run-F2", alert_sinks={})
print("  equal-timestamp second run accepted: code", r_same.code, "changes", len(r_same.outcome.changes))

print("Q6 plausible-but-wrong reading (+60bps, under the 75bps quarantine threshold) and its correction")
swap(svc, url_map({"ASB:term_deposit": asb_with("4.50")}))
r6 = run_operational_cycle(svc, product_type=P, captured_at_utc=T0 + timedelta(days=6), run_id="run-G", alert_sinks={})
snap = svc.get_market_snapshot(product_type=P, now=T0 + timedelta(days=6, hours=1))
print("  accepted unflagged; changes", [(c.describe(), c.suspicious) for c in r6.outcome.changes],
      "ASB12", asb12(svc, T0 + timedelta(days=6, hours=1)), "MARKET_LEADER", resolve_competitor_rate("MARKET_LEADER", snap, 12).rate_pct)
swap(svc, url_map({"ASB:term_deposit": asb_with("3.90")}))
r7 = run_operational_cycle(svc, product_type=P, captured_at_utc=T0 + timedelta(days=7), run_id="run-H", alert_sinks={})
print("  correction next day: changes", [c.describe() for c in r7.outcome.changes])
con = sqlite3.connect(db)
print("  rate_changes ASB 12m history:", con.execute("select change_type, previous_rate_pct, current_rate_pct, change_bps, suspicious, run_id from rate_changes where bank='ASB' and term_months=12 and rate_category='term_deposit' order by id").fetchall())

print("Q7 genuine large reprice (+100bps) re-observed every day: is it ever admitted?")
for day in range(8, 17):
    swap(svc, url_map({"ASB:term_deposit": asb_with("4.90")}))
    r = run_operational_cycle(svc, product_type=P, captured_at_utc=T0 + timedelta(days=day), run_id=f"run-big-{day}", alert_sinks={})
    now = T0 + timedelta(days=day, hours=1)
    snap = svc.get_market_snapshot(product_type=P, now=now)
    print(f"  day {day}: code {r.code} suspicious_keys={len(r.outcome.validation.suspicious_keys)} ASB12 in snapshot={asb12(svc, now)} "
          f"MARKET_LEADER={resolve_competitor_rate('MARKET_LEADER', snap, 12).rate_pct}")
