"""End-to-end trace of one mortgage row and one TD row: fixture -> config -> dashboard_data."""
import sqlite3
from harness import *  # noqa
from balance_pricing_engine.config import load_config
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle
from balance_pricing_engine.pricing_intelligence.strategy import CompetitorRateStrategy, apply_market_strategy_to_config
from balance_pricing_engine.pricing_intelligence import dashboard_data as dd

svc, db = make_service()
for product in ("home_loan", "term_deposit"):
    r = run_operational_cycle(svc, product_type=product, captured_at_utc=T0, alert_sinks={})
    print(product, "code", r.code, r.status, "accepted", r.outcome.rate_count,
          "inserted", r.outcome.write_result.inserted if r.outcome.write_result else None)

con = sqlite3.connect(db); con.row_factory = sqlite3.Row
for bank, prod, term, cat in [("Kiwibank", "home_loan", 12, "special"), ("ASB", "term_deposit", 12, "term_deposit")]:
    row = con.execute("select bank,product_type,rate_category,term_months,rate_pct,interest_payment_frequency,min_balance,"
                      "max_lvr,min_equity,lvr_condition,validation_status,first_observed_at,last_seen_at,content_hash,observation_key "
                      "from observations where bank=? and product_type=? and term_months=? and rate_category=?",
                      (bank, prod, term, cat)).fetchall()
    for x in row: print("DB:", dict(x))

engine = load_config(str(REPO / "configs/base.yaml"))
print("base config competitor_loan_rate", engine.market.competitor_loan_rate,
      "competitor_deposit_rate", engine.market.competitor_deposit_rate,
      "home_loan_reference_rate", engine.market.home_loan_reference_rate,
      "term_deposit_reference_rate", engine.market.term_deposit_reference_rate,
      "our_bank", engine.pricing_intelligence.our_bank if engine.pricing_intelligence else None)
now = T0 + timedelta(hours=1)
hl = svc.get_market_snapshot(product_type="home_loan", now=now)
td = svc.get_market_snapshot(product_type="term_deposit", now=now)
print("HL 12m entries", [(e.bank, e.rate_pct, e.rank) for e in hl.term(12).entries], "median", hl.term(12).median_rate)
print("TD 12m entries", [(e.bank, e.rate_pct, e.rank) for e in td.term(12).entries], "median", td.term(12).median_rate,
      "leader", td.term(12).best_rate)
print("TD tracked terms present", td.term_months)
for strat in ["MARKET_MEDIAN", "MARKET_LEADER"]:
    cfg = apply_market_strategy_to_config(engine, strategy=strat, snapshot=hl, term_months=12,
                                          deposit_snapshot=td, deposit_strategy=strat, deposit_term_months=12)
    print(strat, "-> competitor_loan_rate", cfg.market.competitor_loan_rate, "home_loan_reference_rate", cfg.market.home_loan_reference_rate,
          "competitor_deposit_rate", cfg.market.competitor_deposit_rate, "term_deposit_reference_rate", cfg.market.term_deposit_reference_rate)
cfg_manual = apply_market_strategy_to_config(engine, strategy="MANUAL", snapshot=hl, deposit_snapshot=td)
print("MANUAL returns same object:", cfg_manual is engine)
print(dd.build_term_summary(td, 12))
print(dd.build_ranking_frame(td, 12).to_string())
print(dd.build_market_grid_frame(td).to_string())
print(dd.build_recent_runs_frame(svc.store.get_recent_run_records(product_type="term_deposit") if 'product_type' in svc.store.get_recent_run_records.__code__.co_varnames else svc.store.get_recent_run_records()).to_string())
