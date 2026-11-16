"""Phase 2c after-state for audit cases H and I-b, through the operational interfaces.

Reuses the unedited audit harness (``2026-11-13-evidence/ingest/harness.py``): same fixture
HTML, same banks, same business clock, no network. Run from the repository root:

    python -B docs/audits/2026-11-16-phase-2c-evidence/case_H_I_after.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "2026-11-13-evidence" / "ingest"))
from harness import *  # noqa: E402,F403
from balance_pricing_engine.pricing_intelligence import dashboard_data as dd  # noqa: E402
from balance_pricing_engine.pricing_intelligence.health import assess_system_health  # noqa: E402
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle  # noqa: E402
from balance_pricing_engine.pricing_intelligence.strategy import MarketStrategyError  # noqa: E402

print("===== Case H: service.resolve_competitor_assumption(MARKET_MEDIAN, 12m)")
for product in ("home_loan", "term_deposit"):
    svc, _ = make_service()
    run_operational_cycle(svc, product_type=product, captured_at_utc=T0, alert_sinks={})
    only_anz = url_map({f"{b}:{product}": None for b in BANKS if b != "ANZ"})
    svc._collectors = build_collectors(banks=BANKS, config=svc.config.collection,
                                       fetcher=make_fetcher(only_anz), raw_root=svc._raw_root)
    print(f"-- {product}")
    for day in (1, 6, 8):
        run_operational_cycle(svc, product_type=product, captured_at_utc=T0 + timedelta(days=day), alert_sinks={})
        now = T0 + timedelta(days=day, hours=1)
        try:
            a = svc.resolve_competitor_assumption("MARKET_MEDIAN", 12, now=now, product_type=product)
            c = a.coverage
            print(f" day {day}: {a.rate_pct:.3f}% | eligible {c.eligible_bank_count}/min {c.min_banks} "
                  f"| status {c.status.value} | as_of(newest) {a.as_of:%Y-%m-%d %H:%M} "
                  f"| oldest {c.oldest_observed_at:%Y-%m-%d %H:%M} ({c.oldest_input_age_hours:.0f} h)")
        except MarketStrategyError as exc:
            print(f" day {day}: REFUSED {type(exc).__name__}: {exc}")

print("\n===== Case I-b: seeded TD market, then every TD fetch fails")
svc, _ = make_service()
run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=T0, alert_sinks={})
svc._collectors = build_collectors(banks=BANKS, config=svc.config.collection,
                                   fetcher=make_fetcher(url_map({f"{b}:term_deposit": None for b in BANKS})),
                                   raw_root=svc._raw_root)
for day in (1, 8):
    for d in range(2 if day == 8 else 1, day + 1):
        r = run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=T0 + timedelta(days=d), alert_sinks={})
    now = T0 + timedelta(days=day, hours=1)
    h = assess_system_health(svc, product_type="term_deposit", now=now)
    print(f"-- @T0+{day}d exit {r.code} | run_health {h.run_health.value} | {h.collection_summary}")
    print(f"   fresh_bank_count (rate age) {h.fresh_bank_count} | confirmed_fresh_bank_count {h.confirmed_fresh_bank_count}")
    st = svc.store
    frame = dd.build_freshness_frame(
        st.get_current_observations(product_type="term_deposit"), BANKS, svc.config.max_observation_age_hours, now,
        st.get_current_observation_run_ids(product_type="term_deposit"), st.latest_run_id(product_type="term_deposit"),
        latest_run=st.get_latest_run_record(product_type="term_deposit"),
        collector_health=st.get_collector_health(product_type="term_deposit"),
    )
    print(frame[["Age (hours)", "Status", "Latest attempt status"]].to_string())
loans = assess_system_health(svc, product_type="home_loan", now=T0 + timedelta(days=1, hours=1))
print(f"-- home_loan side unaffected: run_health {loans.run_health.value} ({loans.collection_summary})")
