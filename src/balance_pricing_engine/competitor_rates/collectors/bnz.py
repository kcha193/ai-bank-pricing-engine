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
    live_verified: ClassVar[bool] = False

    special_product_markers: ClassVar[tuple[str, ...]] = ("classic", "special")
    standard_product_markers: ClassVar[tuple[str, ...]] = ("standard",)
    min_equity_product_markers: ClassVar[tuple[str, ...]] = ("classic",)
