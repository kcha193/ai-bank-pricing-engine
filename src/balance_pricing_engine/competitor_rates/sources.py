"""Official source registry for NZ competitor rate pages."""

from __future__ import annotations

from dataclasses import dataclass

from balance_pricing_engine.competitor_rates.models import ProductType


@dataclass(frozen=True)
class RateSource:
    bank: str
    product_type: ProductType
    url: str


OFFICIAL_SOURCES: tuple[RateSource, ...] = (
    RateSource(
        "ANZ",
        ProductType.HOME_LOAN,
        "https://www.anz.co.nz/rates-fees-agreements/home-loans/",
    ),
    RateSource(
        "ANZ",
        ProductType.TERM_DEPOSIT,
        "https://www.anz.co.nz/rates-fees-agreements/term-deposits/",
    ),
    RateSource(
        "ASB",
        ProductType.HOME_LOAN,
        "https://www.asb.co.nz/home-loans-mortgages/interest-rates-fees.html",
    ),
    RateSource(
        "ASB",
        ProductType.TERM_DEPOSIT,
        "https://www.asb.co.nz/term-investments/term-investment-rates.html",
    ),
    RateSource(
        "Westpac",
        ProductType.HOME_LOAN,
        "https://www.westpac.co.nz/home-loans-mortgages/interest-rates/",
    ),
    RateSource(
        "Westpac",
        ProductType.TERM_DEPOSIT,
        "https://www.westpac.co.nz/kiwisaver-investments/term-deposits-pie-investments/"
        "term-deposit-pie-investment-interest-rates/",
    ),
    RateSource(
        "BNZ",
        ProductType.HOME_LOAN,
        "https://www.bnz.co.nz/personal-banking/home-loans/compare-bnz-home-loan-rates",
    ),
    RateSource(
        "BNZ",
        ProductType.TERM_DEPOSIT,
        "https://www.bnz.co.nz/personal-banking/investments/rates",
    ),
    RateSource(
        "Kiwibank",
        ProductType.HOME_LOAN,
        "https://www.kiwibank.co.nz/personal-banking/home-loans/rates-and-fees/",
    ),
    RateSource(
        "Kiwibank",
        ProductType.TERM_DEPOSIT,
        "https://www.kiwibank.co.nz/personal-banking/investments/rates-and-charges/",
    ),
)
