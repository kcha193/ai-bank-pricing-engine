"""BNZ public rate collector."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Optional

from balance_pricing_engine.competitor_rates.collectors.base import BankPricingCollector
from balance_pricing_engine.competitor_rates.extractors import parse_html_rates
from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType


class BNZCollector(BankPricingCollector):
    """BNZ's headline fixed-rate column still needs 20% equity, whatever it is called.

    Until 2026-09, BNZ branded these "Classic" rates -- a word the shared
    extractor's generic header sniffing doesn't recognise, so an unmarked row
    fell through to ``default_fixed_category``. BNZ has since renamed that
    column to "Standard" (confirmed live 2026-09-17: its own "More
    information" tooltip still requires at least 20% equity -- the same
    condition "Classic" used to name; the separate discretionary discount is
    now login-gated and no longer appears in the public HTML at all). Because
    "standard" *is* one of the shared extractor's recognised header words,
    when BNZ publishes only that single column the row is classified
    "standard" before this collector's own special/default-category logic
    ever runs. parse_mortgage_html() below detects that case -- no sibling
    "special"/"classic" tier anywhere in the same page -- and resets the
    hint back to the generic fallback so it resolves the same way an
    unmarked row would. If BNZ ever republishes a genuinely distinct second
    (carded) tier alongside this one, that sibling "special" record makes
    the reset a no-op and "standard" is left to mean what it says.
    """

    bank: ClassVar[str] = "BNZ"
    home_loan_url: ClassVar[str] = (
        "https://www.bnz.co.nz/personal-banking/home-loans/compare-bnz-home-loan-rates"
    )
    term_deposit_url: ClassVar[str] = "https://www.bnz.co.nz/personal-banking/investments/rates"
    #: Verified live 2026-09-17: 7 rates, headline (special) 4.79/4.99/5.29 at
    #: 6/12/18m -- same numbers as the pre-rename "Classic" tier.
    #: Requires --render-javascript; the rate table is client-rendered.
    live_verified: ClassVar[bool] = True

    special_product_markers: ClassVar[tuple[str, ...]] = ("classic", "special")
    # Empty, not ("standard",): BNZ's sole published column header is now the
    # literal word "Standard" (see class docstring), and that word sits in
    # every fixed row's raw_text_context via the header-leak the shared
    # extractor already applied. If this matched it, enrich_mortgage_record's
    # own elif chain would immediately re-derive "standard" from that same
    # leaked text and undo the reset in parse_mortgage_html below. This is
    # safe for a genuine two-tier page too (see
    # test_enrichment_catches_a_bank_specific_header_only_alias): a row the
    # shared extractor already resolved to "standard" via its own header
    # lookup never reaches this elif chain in the first place (it only
    # re-derives when the incoming category is "fixed"/"home_loan").
    standard_product_markers: ClassVar[tuple[str, ...]] = ()
    min_equity_product_markers: ClassVar[tuple[str, ...]] = ("classic",)
    # An unmarked fixed row (post-reset, see parse_mortgage_html) is the sole
    # publicly advertised tier BNZ has left -- the same 20%-equity tier
    # "Classic" used to name.
    default_fixed_category: ClassVar[str] = "special"
    default_fixed_category_implies_min_equity: ClassVar[bool] = True

    def parse_mortgage_html(
        self,
        html: str,
        captured_at_utc: datetime,
        skipped_row_keys: Optional[list[str]] = None,
    ) -> list[CompetitorRateRecord]:
        records = parse_html_rates(
            bank=self.bank,
            product_type=ProductType.HOME_LOAN,
            source_url=self.home_loan_url,
            html=html,
            captured_at_utc=captured_at_utc,
            skipped_row_keys=skipped_row_keys,
        )
        # "special"/"classic" literally present anywhere means BNZ is
        # currently publishing a genuinely distinct discounted tier alongside
        # this one -- checked on raw_text_context (not rate_category, which
        # for the Classic-branded row is still the generic "home_loan" at
        # this point and only becomes "special" once enrich_mortgage_record
        # runs below).
        has_dedicated_special_tier = any(
            marker in record.raw_text_context.lower()
            for record in records
            for marker in self.special_product_markers
        )
        if not has_dedicated_special_tier:
            records = [
                r.model_copy(update={"rate_category": "home_loan"})
                if r.rate_category == "standard"
                else r
                for r in records
            ]
        return [self.enrich_mortgage_record(record) for record in records]
