"""Parse every bank fixture through its real collector parser (no network) and tabulate."""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sys

from balance_pricing_engine.competitor_rates.collectors import COLLECTORS_BY_BANK
from balance_pricing_engine.pricing_intelligence.validation import validate_observations

REPO = __import__('pathlib').Path(__file__).resolve().parents[4]
FX = REPO / "tests/fixtures/pricing_intelligence"
NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)

def nofetch(url):
    raise RuntimeError("network disabled in audit")

cases = []
for bank in ["ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"]:
    slug = bank.lower()
    for name in [f"{slug}_home_loans.html", f"{slug}_home_loans_unmarked.html", f"{slug}_home_loans_marked.html"]:
        if (FX / name).exists():
            cases.append((bank, "home_loan", FX / name))
    for p in [FX / f"{slug}_term_deposits.html", FX / "term_deposits_2026-10-30" / f"{slug}_term_deposits.html"]:
        if p.exists():
            cases.append((bank, "term_deposit", p))

verbose = "-v" in sys.argv
for bank, product, path in cases:
    col = COLLECTORS_BY_BANK[bank](fetcher=nofetch)
    html = path.read_text()
    skipped = []
    parser = col.parse_mortgage_html if product == "home_loan" else col.parse_term_deposit_html
    recs = parser(html, NOW, skipped_row_keys=skipped)
    val = validate_observations(recs)
    cats = Counter(r.rate_category for r in recs)
    print(f"\n== {bank} {product} {path.relative_to(FX)}: {len(recs)} recs, skipped={len(skipped)}, "
          f"accepted={len(val.accepted)} rejected={len(val.rejected)} cats={dict(cats)}")
    for issue in val.issues:
        print("   ISSUE", issue.severity, issue.code, issue.message[:120])
    if verbose:
        for r in recs:
            print(f"   {r.rate_category:16} term={r.term_label_raw!r:>14}->{r.term_months_normalised} d={r.term_days_raw} "
                  f"{r.advertised_rate_pct:5.2f} freq={r.interest_payment_frequency} min={r.min_balance_or_deposit} "
                  f"max={r.max_balance_or_deposit} lvr={r.lvr_condition} maxlvr={r.max_lvr} name={r.product_name!r} "
                  f"cb={r.cashback_text!r} pkg={r.package_requirement!r} eff={r.effective_date_text!r}")
