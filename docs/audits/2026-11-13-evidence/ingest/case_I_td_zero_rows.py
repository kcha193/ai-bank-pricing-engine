"""Challenge case I: term-deposit collection returns zero valid rows."""
import sqlite3, json
from harness import *  # noqa
from balance_pricing_engine.config import load_config
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle, build_summary
from balance_pricing_engine.pricing_intelligence.strategy import apply_market_strategy_to_config, MarketStrategyError
from balance_pricing_engine.pricing_intelligence.health import assess_system_health
from balance_pricing_engine.pricing_intelligence import dashboard_data as dd

ENGINE = load_config(str(REPO / "configs/base.yaml"))
NO_TABLE = "<html><head><title>TD</title></head><body><div id='app'>Loading rates...</div></body></html>"
SPECIAL_HDR = """<html><body><table><tr><th>Term</th><th>Special rate p.a.</th></tr>
<tr><td>6 months</td><td>3.70%</td></tr><tr><td>12 months</td><td>4.10%</td></tr><tr><td>2 years</td><td>4.35%</td></tr></table></body></html>"""
TWO_TIER_TABLES = """<html><body><h3>Under $500,000</h3><table><tr><th>Term</th><th>Interest rate</th></tr>
<tr><td>6 months</td><td>3.60%</td></tr><tr><td>12 months</td><td>4.00%</td></tr></table>
<h3>$500,000 and over</h3><table><tr><th>Term</th><th>Interest rate</th></tr>
<tr><td>6 months</td><td>3.70%</td></tr><tr><td>12 months</td><td>4.10%</td></tr></table></body></html>"""

def report(label, svc, db, result, now):
    con = sqlite3.connect(db)
    s = build_summary(result.outcome, "term_deposit")
    print(f"\n### {label}")
    print(" exit code:", result.code, result.status, "| rates_accepted", s["rates_accepted"], "| rejected", s["rates_rejected"],
          "| banks_failed", s["banks_failed"])
    print(" failure types:", sorted({(f['bank'], f['error_type']) for f in s['failures']}))
    print(" run_record:", result.run_record.status if result.run_record else None,
          "collection_run_id", result.run_record.collection_run_id if result.run_record else None)
    print(" collection_runs rows (TD):", con.execute("select count(*) from collection_run_products where product_type='term_deposit'").fetchone()[0],
          "| operational_runs rows (TD):", con.execute("select count(*), group_concat(status) from operational_runs where product_type='term_deposit'").fetchone())
    print(" collector_health:", con.execute("select bank, consecutive_failures, substr(last_error,1,60) from collector_health where product_type='term_deposit' order by bank").fetchall())
    snap = svc.get_market_snapshot(product_type="term_deposit", now=now)
    t12 = snap.term(12)
    print(" snapshot 12m:", [(e.bank, e.rate_pct, e.observed_at.isoformat()) for e in t12.entries] if t12 else None)
    try:
        cfg = apply_market_strategy_to_config(ENGINE, strategy="MANUAL", deposit_snapshot=snap, deposit_strategy="MARKET_MEDIAN", deposit_term_months=12)
        print(" strategy MARKET_MEDIAN deposit ->", cfg.market.competitor_deposit_rate, "(config default 0.039)")
    except MarketStrategyError as exc:
        print(" strategy MARKET_MEDIAN deposit -> MarketStrategyError:", str(exc)[:140])
    h = assess_system_health(svc, product_type="term_deposit", now=now)
    print(" health: latest_run", h.latest_run.status if h.latest_run else None, "snapshot_valid", h.snapshot_valid,
          "fresh_bank_count", h.fresh_bank_count, [(b.bank, b.health.value, b.collector.value, b.freshness.state.value) for b in h.banks])
    fr = dd.build_freshness_frame(svc.store.get_current_observations(product_type="term_deposit"), BANKS,
                                  svc.config.max_observation_age_hours, now,
                                  svc.store.get_current_observation_run_ids(product_type="term_deposit"),
                                  svc.store.latest_run_id(product_type="term_deposit"))
    print(" freshness frame:\n" + fr.to_string())
    return snap

# I-a: fresh DB, all TD pages render with no table
svc, db = make_service(mapping=url_map({f"{b}:term_deposit": NO_TABLE for b in BANKS}))
r = run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=T0, alert_sinks={})
report("I-a fresh DB, every TD page has no rate table", svc, db, r, T0 + timedelta(hours=1))

# Seeded DB for the rest
for label, overrides in [
    ("I-b seeded, all TD fetches fail (outage)", {f"{b}:term_deposit": None for b in BANKS}),
    ("I-c seeded, every TD table labelled 'Special rate'", {f"{b}:term_deposit": SPECIAL_HDR for b in BANKS}),
    ("I-d seeded, two tier tables per bank distinguished only by <h3>", {f"{b}:term_deposit": TWO_TIER_TABLES for b in BANKS}),
]:
    svc, db = make_service()
    run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=T0, alert_sinks={})
    mapping = url_map(overrides)
    svc._collectors = build_collectors(banks=BANKS, config=svc.config.collection, fetcher=make_fetcher(mapping), raw_root=svc._raw_root)
    t1 = T0 + timedelta(days=1)
    r = run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=t1, alert_sinks={})
    report(label + " @T0+1d", svc, db, r, t1 + timedelta(hours=1))
    if label.startswith("I-b"):
        # keep failing for a week: what does strategy do at T0+8d?
        for d in range(2, 9):
            r = run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=T0 + timedelta(days=d), alert_sinks={})
        report(label + " @T0+8d (7 consecutive failures)", svc, db, r, T0 + timedelta(days=8, hours=1))
    if label.startswith("I-c"):
        cur = svc.store.get_current_observations(product_type="term_deposit")
        print(" I-c stored 'special' TD rows:", sorted({(x.bank, x.term_months_normalised, x.advertised_rate_pct) for x in cur if x.rate_category == 'special'})[:6])
        snap_all = svc.get_market_snapshot(product_type="term_deposit", rate_category="special", now=t1 + timedelta(hours=1))
        print(" I-c special-category TD snapshot 12m:", [(e.bank, e.rate_pct) for e in snap_all.term(12).entries] if snap_all.term(12) else None)
