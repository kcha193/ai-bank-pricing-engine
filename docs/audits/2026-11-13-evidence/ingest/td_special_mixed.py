"""DATA: a term-deposit 'special' row inside an otherwise ordinary TD table is silently excluded from the default TD market."""
from harness import *  # noqa
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle, build_summary
from balance_pricing_engine.pricing_intelligence.strategy import resolve_competitor_rate

asb = TD_FIXTURE["ASB"].read_text().replace(
    "<tr><td>12 Months</td><td>4.00% p.a.</td></tr>",
    "<tr><td>12 Months</td><td>4.00% p.a.</td></tr>\n  <tr><td>12 Months Special</td><td>4.50% p.a.</td></tr>")
assert "Special" in asb
svc, db = make_service(mapping=url_map({"ASB:term_deposit": asb}))
r = run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=T0, alert_sinks={})
s = build_summary(r.outcome, "term_deposit")
print("exit", r.code, r.status, "failures", s["failures"])
asb_rows = [(x.rate_category, x.term_label_raw, x.advertised_rate_pct) for x in r.outcome.validation.accepted if x.bank == "ASB" and x.term_months_normalised == 12]
print("ASB 12m accepted rows:", asb_rows)
snap = svc.get_market_snapshot(product_type="term_deposit", now=T0 + timedelta(hours=1))
print("default TD snapshot 12m:", [(e.bank, e.rate_pct, e.rank) for e in snap.term(12).entries])
lead = resolve_competitor_rate("MARKET_LEADER", snap, 12)
print("MARKET_LEADER deposit benchmark:", lead.rate_pct, lead.rationale, "-- the advertised 4.50% ASB special is not considered")
