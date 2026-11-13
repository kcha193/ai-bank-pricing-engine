"""Challenge case H: competitor observations stale or missing."""
import json, tempfile
from pathlib import Path
from harness import *  # noqa
from balance_pricing_engine.config import load_config
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle
from balance_pricing_engine.pricing_intelligence.strategy import resolve_competitor_rate, MarketStrategyError
from balance_pricing_engine.competitor_rates.models import CompetitorRateRun, CompetitorRatesConfig, ValidationSummary
from balance_pricing_engine.competitor_rates.apply import load_and_resolve_competitor_rates, apply_competitor_rates_to_config, CompetitorRateResolutionError

for product in ("home_loan", "term_deposit"):
    svc, db = make_service()
    run_operational_cycle(svc, product_type=product, captured_at_utc=T0, alert_sinks={})
    # From day 1 onwards only ANZ is reachable
    only_anz = url_map({f"{b}:{product}": None for b in BANKS if b != "ANZ"})
    svc._collectors = build_collectors(banks=BANKS, config=svc.config.collection, fetcher=make_fetcher(only_anz), raw_root=svc._raw_root)
    print(f"\n===== {product}")
    for day in (1, 6, 8):
        r = run_operational_cycle(svc, product_type=product, captured_at_utc=T0 + timedelta(days=day), alert_sinks={})
        now = T0 + timedelta(days=day, hours=1)
        snap = svc.get_market_snapshot(product_type=product, now=now)
        t = snap.term(12)
        print(f" day {day}: exit {r.code}; 12m entries:", [(e.bank, e.rate_pct, round((now - e.observed_at).total_seconds()/3600)) for e in t.entries] if t else None)
        try:
            res = resolve_competitor_rate("MARKET_MEDIAN", snap, 12)
            ages = [round((now - e.observed_at).total_seconds()/3600) for e in t.entries]
            print(f"   MARKET_MEDIAN -> {res.rate_pct:.3f}% as_of={res.as_of.isoformat()} (oldest input {max(ages)}h old) rationale='{res.rationale}'")
        except MarketStrategyError as exc:
            print("   MARKET_MEDIAN -> MarketStrategyError:", exc)
    # all stale
    now = T0 + timedelta(days=30)
    snap = svc.get_market_snapshot(product_type=product, now=now)
    try:
        resolve_competitor_rate("MARKET_MEDIAN", snap, 12)
    except MarketStrategyError as exc:
        print(" day 30 (all stale):", exc)

# Legacy path: stale latest.json
fx = REPO / "tests/fixtures/competitor_rates/latest.json"
run = CompetitorRateRun.model_validate_json(fx.read_text())
print("\n===== legacy competitor_rates: fixture captured_at", run.captured_at_utc, "status", run.validation_summary.status)
tmp = Path(tempfile.mkdtemp(prefix="audit-legacy-")) / "latest.json"
tmp.write_text(run.model_dump_json())
for strict in (True, False):
    cfg = CompetitorRatesConfig(enabled=True, source_file=str(tmp), strict=strict, max_staleness_hours=48)
    try:
        res = load_and_resolve_competitor_rates(cfg)
        print(f" strict={strict}: resolved mortgage {res.mortgage_reference_rate} deposit {res.term_deposit_reference_rate}; warnings={res.validation_warnings[:2]}")
        engine = load_config(str(REPO / "configs/base.yaml"))
        engine = engine.model_copy(update={"competitor_rates": cfg})
        applied = apply_competitor_rates_to_config(engine)
        print(f"   apply_competitor_rates_to_config -> competitor_loan_rate {applied.market.competitor_loan_rate}; returned object carries no warning/age field: "
              f"{[k for k in ('validation_warnings','captured_at_utc') if hasattr(applied.market, k)]}")
    except CompetitorRateResolutionError as exc:
        print(f" strict={strict}: CompetitorRateResolutionError: {str(exc)[:120]}")
