"""BNZ public rate collector."""

from __future__ import annotations

from typing import ClassVar

from balance_pricing_engine.competitor_rates.collectors.base import BankPricingCollector


class BNZCollector(BankPricingCollector):
    """BNZ brands its discounted fixed rates "Classic" rather than "Special".

    Classic rates carry a 20% equity requirement and are not available on
    revolving or interest-only lending, so the Classic marker is what maps a BNZ
    row onto the canonical ``special`` category.
    """

    bank: ClassVar[str] = "BNZ"
    home_loan_url: ClassVar[str] = (
        "https://www.bnz.co.nz/personal-banking/home-loans/compare-bnz-home-loan-rates"
    )
    term_deposit_url: ClassVar[str] = "https://www.bnz.co.nz/personal-banking/investments/rates"
    #: Verified live 2026-08-25: 9 rates, specials 4.79/4.99/5.29 at 6/12/18m.
    #: Requires --render-javascript; the rate table is client-rendered.
    live_verified: ClassVar[bool] = True

    special_product_markers: ClassVar[tuple[str, ...]] = ("classic", "special")
    standard_product_markers: ClassVar[tuple[str, ...]] = ("standard",)
    min_equity_product_markers: ClassVar[tuple[str, ...]] = ("classic",)
    # Verified live 2026-08-25: BNZ's rate rows arrive as "6 months | 4.79% |
    # n/a | n/a | n/a" -- the "Classic" heading never reaches the row text, so
    # the marker above cannot fire and an unmarked row is a Classic special.
    default_fixed_category: ClassVar[str] = "special"
    # BNZ's unmarked row *is* the Classic tier, so it carries the same 20%
    # equity condition "classic" would have implied via min_equity_product_markers.
    default_fixed_category_implies_min_equity: ClassVar[bool] = True
