"""(a) TD snapshot picks the best rate across balance tiers; (b) legacy competitor_rates fetch path skips TD enrichment."""
from harness import *  # noqa
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle
from balance_pricing_engine.competitor_rates.extractors import parse_html_rates
from balance_pricing_engine.competitor_rates.models import CompetitorRateRun, CompetitorRatesConfig, ProductType, SourceAttempt
from balance_pricing_engine.competitor_rates.validate import validate_rate_run
from balance_pricing_engine.competitor_rates.cli import _apply_bank_categorisation
from balance_pricing_engine.competitor_rates.apply import _filter_term_deposit_records, _resolve_strategy, _filter_mortgage_records

TIERED = """<html><body><table><tr><th>Term</th><th>$10,000 - $499,999</th><th>$500,000 - $5,000,000</th></tr>
<tr><td>6 months</td><td>3.60%</td><td>3.85%</td></tr><tr><td>12 months</td><td>4.00%</td><td>4.25%</td></tr></table></body></html>"""
svc, db = make_service(mapping=url_map({"ASB:term_deposit": TIERED}))
r = run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=T0, alert_sinks={})
print("(a) exit", r.code, "ASB 12m rows:", [(x.min_balance_or_deposit, x.max_balance_or_deposit, x.advertised_rate_pct) for x in r.outcome.validation.accepted if x.bank == "ASB" and x.term_months_normalised == 12])
t = svc.get_market_snapshot(product_type="term_deposit", now=T0 + timedelta(hours=1)).term(12)
print("    snapshot 12m:", [(e.bank, e.rate_pct, e.rank) for e in t.entries], "-> ASB represented by its $500k+ tier; leader", t.best_rate)

# (b) legacy fetch path, offline: generic parse + _apply_bank_categorisation (as competitor_rates.cli._fetch does)
html = (FX / "bnz_term_deposits.html").read_text()
recs = parse_html_rates("BNZ", ProductType.TERM_DEPOSIT, "https://example.invalid/bnz", html, T0)
recs = _apply_bank_categorisation(recs)
print("(b) legacy-path BNZ TD records:", [(x.rate_category, x.term_months_normalised, x.advertised_rate_pct, x.raw_text_context[:30]) for x in recs])
cfg = CompetitorRatesConfig(enabled=True, selected_banks=["BNZ"], strict=False)
sel = _filter_term_deposit_records(recs, cfg)
print("    legacy TD selection for BNZ 12m:", [(x.advertised_rate_pct, x.raw_text_context[:40]) for x in sel], "-> market_average", _resolve_strategy(sel, "market_average", None))
run = CompetitorRateRun(run_id="x", captured_at_utc=T0, source_urls_attempted=[], source_attempts=[], validation_summary={"status": "valid", "total_records": 0, "valid_records": 0, "warning_count": 0, "error_count": 0},
                        counts_by_bank_product_type={}, records=recs)
summ = validate_rate_run(run, strict=True)
print("    legacy validate (strict):", summ.status, [i.message[:90] for i in summ.issues if "Duplicate" in i.message][:2])

# (c) legacy mortgage market_average double-counts a bank with several qualifying rows
from balance_pricing_engine.competitor_rates.collectors import COLLECTORS_BY_BANK
hl = []
for b in ["ANZ", "ASB"]:
    hl += COLLECTORS_BY_BANK[b](fetcher=lambda u: b"").parse_mortgage_html(HL_FIXTURE[b].read_text(), T0)
extra = [x.model_copy(update={"product_name": "Fixed Special (package)", "advertised_rate_pct": 4.69}) for x in hl if x.bank == "ANZ" and x.term_months_normalised == 12 and x.rate_category == "special"]
m = _filter_mortgage_records(hl + extra, CompetitorRatesConfig(enabled=True, selected_banks=["ANZ", "ASB"], mortgage={"require_min_20_percent_equity": True}))
print("(c) legacy mortgage records used:", [(x.bank, x.rate_category, x.advertised_rate_pct) for x in m], "market_average", round(_resolve_strategy(m, "market_average", None), 4),
      "(per-bank average would be", round(((4.79 + 4.69) / 2 + 4.89) / 2, 4), "or best-per-bank", round((4.69 + 4.89) / 2, 4), ")")
cfg36 = CompetitorRatesConfig(enabled=True, selected_banks=["BNZ"], strict=False, term_deposit={"term_months": 36})
sel36 = _filter_term_deposit_records(recs, cfg36)
print("(b2) legacy non-strict BNZ 36m TD benchmark:", [(x.advertised_rate_pct, x.raw_text_context[:45]) for x in sel36], "->", _resolve_strategy(sel36, "market_average", None))
