"""Phase 2f evidence: DATA-005 (reference tier), DATA-013 (inferred LVR), DATA-014 (own bank).

Offline: saved fixtures, temporary SQLite. Run from the repository root:
    python docs/audits/2026-11-21-phase-2f-evidence/phase_2f_reproduction.py
Assertive versions live in tests/unit/test_phase_2f_benchmark_comparability.py.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "2026-11-13-evidence" / "ingest"))

from harness import T0, make_service, url_map  # noqa: E402

from balance_pricing_engine.competitor_rates.collectors import COLLECTORS_BY_BANK  # noqa: E402
from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord  # noqa: E402
from balance_pricing_engine.pricing_intelligence.orchestration import (  # noqa: E402
    run_operational_cycle,
)
from balance_pricing_engine.pricing_intelligence.strategy import (  # noqa: E402
    resolve_competitor_rate,
)

TIERED = """<html><body><table><tr><th>Term</th><th>$10,000 - $499,999</th><th>$500,000 - $5,000,000</th></tr>
<tr><td>6 months</td><td>3.60%</td><td>3.85%</td></tr><tr><td>12 months</td><td>4.00%</td><td>4.25%</td></tr></table></body></html>"""


def td_market(**config):
    svc, _ = make_service(mapping=url_map({"ASB:term_deposit": TIERED}), **config)
    run_operational_cycle(svc, product_type="term_deposit", captured_at_utc=T0, alert_sinks={})
    return svc


print("DATA-005 reference tier")
for label, config in (("$10,000", {}), ("$500,000", {"deposit_reference": {"balance_amount": 500_000}})):
    svc = td_market(**config)
    term = svc.get_market_snapshot(product_type="term_deposit", now=T0 + timedelta(hours=1)).term(12)
    print(f"  reference {label}: 12m", [(e.bank, e.rate_pct, e.rank) for e in term.entries],
          "best", term.best_rate, "median", term.median_rate)
    print("    eligibility:", {e.bank: e.eligibility_note for e in term.entries})
raw = svc.get_market_snapshot(product_type="term_deposit", now=T0 + timedelta(hours=1), all_tiers=True)
print("  all-tier descriptive view (deposit_reference=None):",
      [(e.bank, e.rate_pct) for e in raw.term(12).entries])

print("DATA-013 mortgage eligibility provenance")
for bank, text in (
    ("ANZ", "Fixed Special | 1 year | 4.79%"),
    ("Westpac", "Fixed | 1 year | 4.89% | 80% LVR"),
    ("Westpac", "Fixed | 1 year | 5.49%"),
):
    record = CompetitorRateRecord(
        bank=bank, product_type="home_loan", product_name="Fixed", rate_category="fixed",
        term_label_raw="1 year", term_months_normalised=12, advertised_rate_pct=4.79,
        source_url="https://example.invalid", captured_at_utc=T0, raw_text_context=text,
        raw_html_sha256="c" * 64,
    )
    record = COLLECTORS_BY_BANK[bank](fetcher=lambda url: b"").enrich_mortgage_record(record)
    print(f"  {bank} {text!r}: max_lvr={record.max_lvr} basis={record.lvr_eligibility_basis()} "
          f"eligibility_inferred={record.eligibility_inferred}")

print("DATA-014 own-bank identity")
svc = td_market()
snapshot = svc.get_market_snapshot(product_type="term_deposit", now=T0 + timedelta(hours=1))
for name in ("ASB", "ASB Bank"):
    r = resolve_competitor_rate("MARKET_MEDIAN", snapshot, 12, our_bank=name, now=T0)
    print(f"  our_bank={name!r}: median {r.rate_pct:.4f} banks {r.source_banks} "
          f"coverage {r.coverage.eligible_banks}")
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig  # noqa: E402

print("  PricingIntelligenceConfig(our_bank='ASB Bank').our_bank =",
      repr(PricingIntelligenceConfig(our_bank="ASB Bank").our_bank))
try:
    PricingIntelligenceConfig(our_bank="ASB Banking Group")
except ValueError as exc:
    print("  our_bank='ASB Banking Group' ->", type(exc).__name__, str(exc).splitlines()[-2].strip())
