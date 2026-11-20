"""Phase 2d after-evidence for DATA-006, DATA-018 and SEC-010 (offline, synthetic fixtures).

Complements the unmodified audit scripts in ../2026-11-13-evidence/ingest/. Run from the
repository root: ``python docs/audits/2026-11-17-phase-2d-evidence/legacy_after.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from balance_pricing_engine.competitor_rates.apply import (
    _resolve_strategy,
    apply_competitor_rates_with_resolution,
    load_and_resolve_competitor_rates,
)
from balance_pricing_engine.competitor_rates.cli import _apply_bank_categorisation
from balance_pricing_engine.competitor_rates.collectors import COLLECTORS_BY_BANK
from balance_pricing_engine.competitor_rates.extractors import parse_html_rates
from balance_pricing_engine.competitor_rates.models import (
    CompetitorRateRun, CompetitorRatesConfig, ProductType, SourceAttempt, ValidationSummary,
)
from balance_pricing_engine.competitor_rates.sources import OFFICIAL_SOURCES
from balance_pricing_engine.competitor_rates.store import load_rate_run, write_rate_run
from balance_pricing_engine.competitor_rates.validate import validate_rate_run
from balance_pricing_engine.config import load_config

T0 = datetime(2026, 11, 10, 18, 30, tzinfo=timezone.utc)
FX = Path("tests/fixtures/pricing_intelligence")
BANKS = ["ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"]


def legacy(bank, product, path):
    records = parse_html_rates(bank, product, f"https://example.invalid/{bank}", path.read_text(), T0)
    return _apply_bank_categorisation(records)


def run_of(records, attempts):
    return CompetitorRateRun(
        run_id="phase-2d", captured_at_utc=T0, source_urls_attempted=[], source_attempts=attempts,
        validation_summary=ValidationSummary(status="valid", total_records=len(records),
                                             valid_records=len(records), warning_count=0,
                                             error_count=0),
        counts_by_bank_product_type={}, records=records,
    )


print("== B. strict validation")
bnz = legacy("BNZ", ProductType.TERM_DEPOSIT, FX / "bnz_term_deposits.html")
summary = validate_rate_run(run_of(bnz, []), strict=True)
print(" audit run (BNZ TD only, no source attempts):", summary.status,
      sorted({i.message.split(" for ")[0] for i in summary.issues if i.severity == "error"}))
complete = [r for b in BANKS for r in
            legacy(b, ProductType.HOME_LOAN, FX / f"{b.lower()}_home_loans.html")
            + legacy(b, ProductType.TERM_DEPOSIT,
                     FX / "term_deposits_2026-10-30" / f"{b.lower()}_term_deposits.html")]
attempts = [SourceAttempt(bank=s.bank, product_type=s.product_type, source_url=s.url,
                          success=True, records_extracted=1) for s in OFFICIAL_SOURCES]
summary = validate_rate_run(run_of(complete, attempts), strict=True)
print(f" complete 5-bank HL+TD fixture run ({len(complete)} records):", summary.status,
      "errors:", summary.error_count)

print("== D. mortgage market_average, one representative per bank")
hl = [r for b in ("ANZ", "ASB") for r in COLLECTORS_BY_BANK[b](fetcher=lambda u: b"")
      .parse_mortgage_html((FX / f"{b.lower()}_home_loans.html").read_text(), T0)
      if r.term_months_normalised == 12 and r.rate_category == "special"
      and r.lvr_condition == "min_20_percent_equity"]
base = hl
for label, rate in (("cheaper extra ANZ row 4.69", 4.69), ("dearer extra ANZ row 4.99", 4.99)):
    extra = [r.model_copy(update={"product_name": "Fixed Special (package)",
                                  "advertised_rate_pct": rate})
             for r in base if r.bank == "ANZ"]
    rows = base + extra
    row_weighted = sum(r.advertised_rate_pct for r in rows) / len(rows)
    print(f" {label}: rows={[(r.bank, r.advertised_rate_pct) for r in rows]} "
          f"row-weighted={row_weighted:.4f} per-bank={_resolve_strategy(rows, 'market_average', None):.4f}")

print("== H. non-strict stale legacy source")
tmp = Path(tempfile.mkdtemp(prefix="phase-2d-"))
fixture = load_rate_run("tests/fixtures/competitor_rates/latest.json")
(tmp / "latest.json").write_text(fixture.model_dump_json())
cfg = CompetitorRatesConfig(enabled=True, source_file=str(tmp / "latest.json"), strict=False,
                            max_staleness_hours=48)
now = fixture.captured_at_utc + timedelta(hours=100)
resolved = load_and_resolve_competitor_rates(cfg, now=now)
print(" resolution: stale", resolved.stale, "age_h", resolved.source_age_hours,
      "max_h", resolved.max_staleness_hours, "status", resolved.source_validation_status)
engine = load_config("configs/base.yaml").model_copy(update={"competitor_rates": cfg})
applied, res = apply_competitor_rates_with_resolution(engine, now=now)
print(" overlay: competitor_loan_rate", applied.market.competitor_loan_rate,
      "| provenance stale", res.provenance()["stale"], "| warnings", res.validation_warnings)

print("== SEC-010. latest.csv neutralisation")
evil = fixture.records[0].model_copy(update={"product_name": "=HYPERLINK(\"https://example.invalid\")",
                                             "raw_text_context": "\t@SUM(1,2)"})
write_rate_run(fixture.model_copy(update={"records": [evil]}), tmp / "out")
csv_text = (tmp / "out" / "latest.csv").read_text()
print(" csv product_name cell:", [line for line in csv_text.splitlines() if "HYPERLINK" in line][0][:80])
print(" json keeps original:", load_rate_run(tmp / "out" / "latest.json").records[0].product_name)
