"""Product identity for the two markets the console tracks. Pure data: no Streamlit, no maths.

Home loans (lending) and term deposits (funding) are ranked in opposite directions -- a borrower
wants the lowest rate, a depositor the highest -- and the backend already ranks them that way
(``TermMarket.best_rate``, competition ranks). This module only names things: every page reads
its product title, ranking rule, icon and accent from here, so no rate is ever shown without
saying which side of the balance sheet it belongs to.

Colour is never the only carrier of identity: each profile pairs its accent with a symbol, an
icon and a word ("⌂ Home loans", "◎ Term deposits").
"""

from __future__ import annotations

from dataclasses import dataclass

from balance_pricing_engine.competitor_rates.models import ProductType


@dataclass(frozen=True)
class ProductProfile:
    product_type: ProductType
    label: str  # "Home loans" -- selector and badges
    market: str  # "Home Loan" -- page titles ("Home Loan Market Explorer")
    noun: str  # "mortgage" -- in sentences ("the 1Y mortgage market")
    rate_noun: str  # "mortgage rate"
    side: str  # balance-sheet side: "Lending" / "Deposits"
    tier: str  # the comparable tier rankings use
    customer: str  # whose view "better" is from
    best_word: str  # "lowest" / "highest"
    better: str  # "lower" / "higher"
    icon: str  # Streamlit material icon
    symbol: str  # text marker, so identity never relies on colour
    accent: str  # hex accent for charts and panel rules
    badge: str  # ``st.badge`` colour name
    #: What a more attractive rate means for the bank. Customer appeal is not bank margin.
    trade_off: str

    @property
    def ranking_rule(self) -> str:
        """e.g. "Mortgage rates: lower is more attractive to borrowers"."""
        return f"{self.rate_noun.capitalize()}s: {self.better} is more attractive to {self.customer}"

    @property
    def leader_label(self) -> str:
        """e.g. "Mortgage market leader — lowest comparable rate"."""
        return f"{self.noun.capitalize()} market leader — {self.best_word} comparable rate"

    @property
    def tag(self) -> str:
        return f"{self.symbol} {self.label}"

    def title(self, page: str) -> str:
        return f"{self.market} {page}"


PRODUCTS: dict[ProductType, ProductProfile] = {
    ProductType.HOME_LOAN: ProductProfile(
        product_type=ProductType.HOME_LOAN,
        label="Home loans",
        market="Home Loan",
        noun="mortgage",
        rate_noun="mortgage rate",
        side="Lending",
        tier="advertised fixed specials",
        customer="borrowers",
        best_word="lowest",
        better="lower",
        icon=":material/home:",
        symbol="⌂",
        accent="#1F5AA6",
        badge="blue",
        trade_off=(
            "A lower mortgage rate attracts borrowers but narrows the bank's lending margin."
        ),
    ),
    ProductType.TERM_DEPOSIT: ProductProfile(
        product_type=ProductType.TERM_DEPOSIT,
        label="Term deposits",
        market="Term Deposit",
        noun="term deposit",
        rate_noun="deposit rate",
        side="Deposits",
        tier="standard term deposits (PIE returns, managed funds and KiwiSaver excluded)",
        customer="depositors",
        best_word="highest",
        better="higher",
        icon=":material/savings:",
        symbol="◎",
        accent="#0E8C8C",
        badge="green",
        trade_off=(
            "A higher deposit rate attracts depositors but raises the bank's funding cost; "
            "the market leader for depositors is not the best rate for bank margin."
        ),
    ),
}

#: Display order everywhere both products appear side by side.
PRODUCT_ORDER: tuple[ProductType, ...] = (ProductType.HOME_LOAN, ProductType.TERM_DEPOSIT)


def profile(product: ProductType | str) -> ProductProfile:
    return PRODUCTS[ProductType(product)]
