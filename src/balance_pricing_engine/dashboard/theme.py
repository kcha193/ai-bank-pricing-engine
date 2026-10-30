"""Design tokens for the Pricing Watch console. Pure data: no Streamlit, no market maths.

One place defines the palette, the semantic colours and the scoped CSS, so every page and
chart reads as one product. Colours are mid-tones chosen to stay legible on both the light and
the dark Streamlit themes (``.streamlit/config.toml`` sets the base palette); CSS surfaces and
chart text follow the active native theme through the shared presentation components.

Colour is never the only carrier of meaning: every semantic colour below is paired with a
symbol or word wherever it is used (``▲ Up``, ``◆ Leader``, ``⚠ Stale``).
"""

from __future__ import annotations

from typing import Iterable, Mapping

# ----------------------------------------------------------------------------- palette

NAVY = "#0B1F3A"
BLUE = "#1E40AF"
CHART_BLUE = "#4C78A8"
DARK_TEXT = "#E6ECF5"
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
    CHART_BLUE,  # blue, legible on both themes
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
ROLE_COLOURS = {"Leader": TEAL, "Selected": CHART_BLUE, "Other": SLATE}

#: Event direction. Rate rises and cuts are not "good" or "bad" here -- a competitor's cut is
#: good for borrowers and a threat to a lender -- so they use a neutral blue/orange pair.
DIRECTION_COLOURS = {
    "Increase": RED,
    "Decrease": CHART_BLUE,
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
CHART_INK = "#607D9F"  # readable series on both native theme backgrounds
BANK_DASHES = ([1, 0], [7, 3], [2, 3], [10, 3, 2, 3], [5, 2, 1, 2], [12, 4], [3, 2], [8, 2])
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

#: App-owned container keys and Streamlit test-id hooks; never generated class names.
#: Native controls retain their semantics and keyboard behaviour.
APP_CSS = """
<style>
:root {
  --pw-radius: .75rem;
  --pw-border: rgba(124, 141, 166, .28);
  --pw-lending: #1E40AF; --pw-deposit: #0E8C8C;
  --pw-lending-soft: rgba(30, 64, 175, .07);
  --pw-deposit-soft: rgba(14, 140, 140, .09);
  --pw-space-1: 8px; --pw-space-2: 12px; --pw-space-3: 16px;
  --pw-space-4: 24px; --pw-space-5: 32px;
  --pw-font-sans: 'Fira Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --pw-font-data: 'Fira Code', 'SFMono-Regular', Consolas, monospace;
}
[data-testid="stAppViewContainer"] { font-family: var(--pw-font-sans); }
[data-testid="stMainBlockContainer"] { max-width: 1520px; padding-top: 5.2rem; }
[data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] { gap: 1.3rem; }
.st-key-pw-brand { border-bottom: 1px solid var(--pw-border); padding-bottom: 1.15rem; }
.st-key-pw-brand h1 { padding: 0 !important; margin: 0 !important; font-weight: 700 !important; }
.st-key-pw-brand [data-testid="stHeaderActionElements"] { display: none; }
.st-key-pw-brand [data-testid="stCaptionContainer"] p { margin: 0; }
.st-key-pw-page-header [data-testid="stCaptionContainer"] { font-size: .95rem; }
[data-testid="stHeadingWithActionElements"] h3 {
  font-size: 1.1rem !important; font-weight: 650 !important; padding-top: .5rem !important;
}
[data-testid="stMetricValue"], [data-testid="stDataFrame"], .pw-num { font-variant-numeric: tabular-nums; }
.st-key-pw-insights [data-testid="stMarkdownContainer"] li { margin-bottom: .35rem; }
.st-key-pw-banner-home_loan, .st-key-pw-banner-term_deposit {
  border-left: 4px solid var(--pw-lending); background: var(--pw-lending-soft);
  border-radius: var(--pw-radius); padding: .6rem .9rem;
}
.st-key-pw-banner-term_deposit { border-left-color: var(--pw-deposit); background: var(--pw-deposit-soft); }
.st-key-pw-banner-home_loan [data-testid="stCaptionContainer"] p,
.st-key-pw-banner-term_deposit [data-testid="stCaptionContainer"] p { margin: 0; }
.st-key-pw-panel-home_loan { border-top: 3px solid var(--pw-lending); }
.st-key-pw-panel-term_deposit { border-top: 3px solid var(--pw-deposit); }
.st-key-pw-filters { border: 1px solid var(--pw-border); border-radius: var(--pw-radius); }
.st-key-pw-brand h1 { font-size: 1.1rem !important; letter-spacing: -.025em; }
.st-key-pw-page-header { gap: .25rem; margin: .35rem 0 .4rem; }
.st-key-pw-page-header > :first-child p {
  font: 600 .7rem var(--pw-font-data); letter-spacing: .13em;
}
.st-key-pw-page-header h2 {
  font-size: clamp(1.7rem, 2.5vw, 2.3rem) !important;
  line-height: 1.15; letter-spacing: -.045em; padding-top: .2rem !important;
}
.st-key-pw-page-header > :last-child p { max-width: 76ch; line-height: 1.6; }
[data-testid="stMetric"] { padding: 1rem; background: var(--pw-surface); }
[data-testid="stMetricLabel"] p {
  font-size: .75rem !important; opacity: 1; letter-spacing: .025em;
  text-transform: none; line-height: 1.5; white-space: normal;
}
[data-testid="stMetricValue"] {
  font-family: var(--pw-font-data); font-size: 1.6rem !important;
  letter-spacing: -.045em; line-height: 1.35;
}
[data-testid="stMetricValue"] > div { white-space: normal; overflow-wrap: anywhere; }
.st-key-pw-panel-home_loan, .st-key-pw-panel-term_deposit {
  background: var(--pw-surface); border-top-width: 3px; padding: .5rem;
}
.st-key-pw-panel-home_loan h3, .st-key-pw-panel-term_deposit h3 { font-size: 1.2rem !important; }
.st-key-pw-panel-home_loan [data-testid="stMetric"],
.st-key-pw-panel-term_deposit [data-testid="stMetric"] { padding: .5rem; }
.st-key-pw-panel-home_loan [data-testid="stMetricValue"],
.st-key-pw-panel-term_deposit [data-testid="stMetricValue"] { font-size: 1.4rem !important; }
[class*="st-key-pw-metrics-3-headline-"] [data-testid="stColumn"]:first-child [data-testid="stMetricValue"] {
  font-size: 2.35rem !important; letter-spacing: -.06em;
}
[class*="st-key-pw-metrics-"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {
  display: grid; gap: .75rem;
}
[class*="st-key-pw-metrics-3-"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
[class*="st-key-pw-metrics-5-"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {
  grid-template-columns: repeat(5, minmax(0, 1fr));
}
[class*="st-key-pw-metrics-6-"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {
  grid-template-columns: repeat(6, minmax(0, 1fr));
}
[class*="st-key-pw-metrics-"] [data-testid="stColumn"] { width: 100%; min-width: 0; }
[class*="st-key-pw-chart-"] { background: var(--pw-surface); border-radius: var(--pw-radius); }
.st-key-pw-filters { padding: .9rem 1rem .4rem; background: var(--pw-filter); }
.st-key-pw-filters [data-testid="stCaptionContainer"] p { font-size: .8rem; }
[data-testid="stTabs"] button[role="tab"] { min-height: 44px; padding: .5rem .85rem; }
[data-testid="stButton"] button, [data-testid="stLinkButton"] a,
[data-testid="stPageLink"] a, [class*="st-key-pw-product-switch"] button {
  min-height: 44px; transition: background-color 160ms ease, border-color 160ms ease;
}
[class*="st-key-pw-product-switch"] button { padding: .6rem .9rem; letter-spacing: .02em; }
.st-key-pw-workflow { background: var(--pw-filter); }
.st-key-pw-workflow [data-testid="stCaptionContainer"] p { margin-bottom: 0; }
.st-key-pw-feed [data-testid="stVerticalBlockBorderWrapper"] { background: var(--pw-surface); }
a:focus-visible, button:focus-visible, input:focus-visible, [role="combobox"]:focus-visible {
  outline: 3px solid var(--pw-focus) !important; outline-offset: 3px;
}
@media (max-width: 1100px) {
  .st-key-pw-markets > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
  .st-key-pw-filters > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {
    flex-wrap: wrap;
  }
  .st-key-pw-markets > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
    flex: 1 1 100%; min-width: 0;
  }
  [class*="st-key-pw-metrics-5-"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
  [class*="st-key-pw-metrics-6-"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 800px) {
  [data-testid="stMainBlockContainer"] { padding-left: 1.25rem; padding-right: 1.25rem; }
  .st-key-pw-filters [data-testid="stColumn"] { flex: 1 1 45%; min-width: 0; }
  .st-key-pw-brand [data-testid="stCaptionContainer"] p { font-size: .75rem; }
}
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"] { padding: 4.5rem 1rem 2rem; }
  [class*="st-key-pw-metrics-"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {
    grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .5rem;
  }
  [data-testid="stMetricValue"] { font-size: 1.4rem !important; }
  [data-testid="stMetric"] { padding: .75rem; }
  .st-key-pw-filters [data-testid="stColumn"] { flex: 1 1 100%; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition-duration: 0ms !important; animation-duration: 0ms !important; }
}
</style>
"""


def app_css(*, dark: bool = False) -> str:
    """Semantic surfaces follow the active native theme; widgets retain native semantics."""
    surface, filters, focus = (
        ("#131D31", "#17233A", "#7CC7E6") if dark else ("#FFFFFF", "#E9EEF6", "#1E40AF")
    )
    tokens = (
        f"<style>:root {{ --pw-surface: {surface}; --pw-filter: {filters}; "
        f"--pw-focus: {focus}; }}</style>"
    )
    return tokens + APP_CSS
