"""Design tokens for the Pricing Watch console. Pure data: no Streamlit, no market maths.

One place defines the palette, the semantic colours and the scoped CSS, so every page and
chart reads as one product. Colours are mid-tones chosen to stay legible on both the light and
the dark Streamlit themes (``.streamlit/config.toml`` sets the base palette); CSS uses
translucent fills and ``currentColor`` rather than hard-coded text colours for the same reason.

Colour is never the only carrier of meaning: every semantic colour below is paired with a
symbol or word wherever it is used (``▲ Up``, ``◆ Leader``, ``⚠ Stale``).
"""

from __future__ import annotations

from typing import Iterable, Mapping

# ----------------------------------------------------------------------------- palette

NAVY = "#0B1F3A"
BLUE = "#1F5AA6"
TEAL = "#0E8C8C"
SLATE = "#7C8DA6"
MUTED = "#A9B6C8"
AMBER = "#C77C02"
RED = "#C2410C"
GREEN = "#2F855A"

#: Categorical bank colours, assigned in configured bank order so a bank keeps its colour on
#: every chart. Eight distinguishable mid-tones; ordered so the first five (the default NZ
#: market) are maximally separated.
BANK_PALETTE = (
    "#1F5AA6",  # blue
    "#E07B39",  # orange
    "#0E8C8C",  # teal
    "#8E5BB5",  # violet
    "#C9A227",  # gold
    "#4F8A4C",  # green
    "#D45087",  # rose
    "#6C7A89",  # slate
)

#: Roles on the ranking chart. The leader is teal, the highlighted bank blue; everyone else
#: recedes. A bank that is both is drawn as "Selected" and keeps a leader marker in its label.
ROLE_COLOURS = {"Leader": TEAL, "Selected": BLUE, "Other": MUTED}

#: Event direction. Rate rises and cuts are not "good" or "bad" here -- a competitor's cut is
#: good for borrowers and a threat to a lender -- so they use a neutral blue/orange pair.
DIRECTION_COLOURS = {
    "Increase": RED,
    "Decrease": BLUE,
    "New product": GREEN,
    "Withdrawn": SLATE,
    "Other change": AMBER,
}
DIRECTION_SYMBOLS = {
    "Increase": "▲",
    "Decrease": "▼",
    "New product": "＋",
    "Withdrawn": "－",
    "Other change": "•",
}
#: Backend ``ChangeType`` value -> presentation group.
CHANGE_GROUPS = {
    "RATE_INCREASE": "Increase",
    "RATE_DECREASE": "Decrease",
    "NEW_PRODUCT": "New product",
    "REMOVED_PRODUCT": "Withdrawn",
    "ELIGIBILITY_CHANGE": "Other change",
    "PROMOTION_CHANGE": "Other change",
}
#: Streamlit badge colours for the same groups (``st.badge`` accepts a fixed set of names).
CHANGE_BADGE_COLOURS = {
    "Increase": "orange",
    "Decrease": "blue",
    "New product": "green",
    "Withdrawn": "gray",
    "Other change": "violet",
}

#: Market-state label -> (badge colour, plain-English meaning).
MARKET_STATE_STYLE = {
    "LOW": ("gray", "Quiet market"),
    "NORMAL": ("blue", "Normal activity"),
    "ELEVATED": ("orange", "Elevated activity"),
    "HIGH": ("red", "High activity"),
}

CHART_HEIGHT = 300
CHART_ROW_HEIGHT = 44  # per bank on ranking charts: readable labels without dead space
#: Charts are autosized to fit, so a declared height includes legend and axis; add this much.
CHART_CHROME = 90
AXIS_LABEL_SIZE = 12
AXIS_TITLE_SIZE = 12


def change_group(change_type: str) -> str:
    return CHANGE_GROUPS.get(change_type, "Other change")


def bank_colours(banks: Iterable[str]) -> Mapping[str, str]:
    """A stable bank -> colour mapping in configured order (cycles past eight banks)."""
    return {bank: BANK_PALETTE[i % len(BANK_PALETTE)] for i, bank in enumerate(banks)}


# --------------------------------------------------------------------------------- CSS

#: Scoped CSS, injected once per page view. Targets Streamlit's stable ``data-testid`` hooks and
#: the ``st-key-pw-*`` classes Streamlit derives from container keys -- never generated class
#: names. Purely presentational: nothing here hides or reorders information.
APP_CSS = """
<style>
:root {
  --pw-radius: 0.75rem;
  --pw-border: rgba(124, 141, 166, 0.28);
  --pw-soft: rgba(31, 90, 166, 0.06);
  --pw-teal-soft: rgba(14, 140, 140, 0.12);
  --pw-amber-soft: rgba(199, 124, 2, 0.13);
  --pw-muted: rgba(124, 141, 166, 1);
  --pw-lending: #1F5AA6;
  --pw-lending-soft: rgba(31, 90, 166, 0.09);
  --pw-deposit: #0E8C8C;
  --pw-deposit-soft: rgba(14, 140, 140, 0.10);
}

/* Layout: a calmer, wider canvas with consistent rhythm. */
[data-testid="stMainBlockContainer"] {
  padding-top: 4.5rem;
  padding-bottom: 3rem;
  max-width: 1480px;
}
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"] { padding-left: 1rem; padding-right: 1rem; }
}

/* Numbers line up in tables and metrics. */
[data-testid="stMetricValue"], [data-testid="stDataFrame"], .pw-num {
  font-variant-numeric: tabular-nums;
}

/* Brand bar. The product title is identity, not a page heading. */
.st-key-pw-brand h1 {
  font-size: 1.05rem !important;
  font-weight: 700 !important;
  letter-spacing: 0.02em;
  padding: 0 !important;
  margin: 0 !important;
}
.st-key-pw-brand [data-testid="stHeaderActionElements"] { display: none; }
.st-key-pw-brand {
  border-bottom: 1px solid var(--pw-border);
  padding-bottom: 0.6rem;
  margin-bottom: 0.4rem;
}
.st-key-pw-brand [data-testid="stCaptionContainer"] p { margin: 0; }

/* Page header. */
.st-key-pw-page-header h2 {
  font-size: 1.85rem !important;
  font-weight: 700 !important;
  padding-bottom: 0.1rem !important;
}
.st-key-pw-page-header [data-testid="stCaptionContainer"] { font-size: 0.98rem; }

/* Section headings: smaller, tighter, with a quiet rule. */
[data-testid="stHeadingWithActionElements"] h3 {
  font-size: 1.15rem !important;
  font-weight: 650 !important;
  padding-top: 0.6rem !important;
}

/* Cards: bordered containers and metrics share one card language. */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {
  border-radius: var(--pw-radius);
}
[data-testid="stMetric"] {
  border-radius: var(--pw-radius);
}
[data-testid="stMetricLabel"] p {
  font-size: 0.82rem !important;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  opacity: 0.78;
}
[data-testid="stMetricValue"] {
  font-size: 1.75rem !important;
  font-weight: 650 !important;
}

/* Event feed rows. */
.st-key-pw-feed [data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--pw-soft);
}

/* Insight list. */
.st-key-pw-insights [data-testid="stMarkdownContainer"] li { margin-bottom: 0.35rem; }

/* Filter bar: one tinted strip, so controls read as a unit. */
.st-key-pw-filters {
  background: var(--pw-soft);
  border: 1px solid var(--pw-border);
  border-radius: var(--pw-radius);
  padding: 0.6rem 0.9rem 0.2rem 0.9rem;
}

/* Tabs: larger hit targets, clear active state. */
[data-testid="stTabs"] button[role="tab"] { padding: 0.4rem 0.9rem; }
[data-testid="stTabs"] button[role="tab"] p { font-size: 0.98rem; font-weight: 600; }

/* Product identity. Lending is blue/navy, deposits teal/green; every accent is paired with a
   symbol and a product name in the markup, so colour is never the only signal. */
.st-key-pw-banner-home_loan, .st-key-pw-banner-term_deposit {
  border-left: 5px solid var(--pw-lending);
  background: var(--pw-lending-soft);
  border-radius: var(--pw-radius);
  padding: 0.45rem 0.9rem;
}
.st-key-pw-banner-term_deposit {
  border-left-color: var(--pw-deposit);
  background: var(--pw-deposit-soft);
}
.st-key-pw-banner-home_loan [data-testid="stCaptionContainer"] p,
.st-key-pw-banner-term_deposit [data-testid="stCaptionContainer"] p { margin: 0; }
.st-key-pw-panel-home_loan, .st-key-pw-panel-term_deposit {
  border-top: 5px solid var(--pw-lending);
  border-radius: var(--pw-radius);
  padding-top: 0.2rem;
}
.st-key-pw-panel-term_deposit { border-top-color: var(--pw-deposit); }
.st-key-pw-panel-home_loan [data-testid="stMetricLabel"] p,
.st-key-pw-panel-term_deposit [data-testid="stMetricLabel"] p {
  font-size: 0.72rem !important;
  letter-spacing: 0.02em;
}
.st-key-pw-panel-home_loan [data-testid="stMetricValue"],
.st-key-pw-panel-term_deposit [data-testid="stMetricValue"] { font-size: 1.5rem !important; }
.st-key-pw-brand { padding-bottom: 0.9rem; }
[class*="st-key-pw-product-switch"] button {
  padding: 0.5rem 1.15rem;
  font-weight: 700;
  letter-spacing: 0.05em;
}
[class*="st-key-pw-product-switch"] button p { font-size: 0.95rem; font-weight: 700; }

/* Keyboard focus is always visible. */
a:focus-visible, button:focus-visible, [role="tab"]:focus-visible {
  outline: 2px solid #0E8C8C !important;
  outline-offset: 2px;
}
</style>
"""
