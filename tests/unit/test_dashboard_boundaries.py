"""Architecture invariants for the dashboard.

The dashboard formats and renders; it must not recompute pricing intelligence, must not let a
read path reach an operational action, and must not push untrusted text through Markdown.
"""

import ast
from pathlib import Path

import pytest

SRC = Path("src/balance_pricing_engine")
DASH = SRC / "dashboard"
PAGES = sorted((DASH / "pages").glob("*.py"))

#: Modules that must stay importable and testable without Streamlit.
STREAMLIT_FREE = [
    "services",
    "actions",
    "formatting",
    "charts",
    "models",
    "results",
    "settings",
    "scenario_text",
    "run_worker",
    "guard",
]

#: Backend computations a page must not re-implement or call directly (it reads them from
#: ``DashboardServices``). ``scenario_lab`` is the legacy YAML runner and calls the optimiser itself.
RECOMPUTATION_TOKENS = (
    "import statistics",
    "from statistics",
    "median(",
    ".rank_for(",
    "rate_change_bps",
    "is_stale",
    "collector_status",
    "assess_observation_freshness",
    "term_market_from_rates",
    "build_market_snapshot",
    "classify_market_state",
    "compute_competitive_position",
)

#: Streamlit calls whose string arguments are interpreted as Markdown.
MARKDOWN_SURFACES = {
    "markdown",
    "write",
    "info",
    "warning",
    "error",
    "success",
    "caption",
    "metric",
    "subheader",
    "header",
    "title",
}

#: Record/result fields that carry text from bank pages or from error messages.
UNTRUSTED_ATTRIBUTES = {
    "product_name",
    "effective_date_text",
    "parser_version",
    "source_url",
    "last_error",
    "error_summary",
    "raw_text_context",
    "special_conditions",
    "promotion_text",
    "cashback_text",
    "term_label_raw",
    "source_bank_page_title",
}


@pytest.mark.parametrize("module", STREAMLIT_FREE)
def test_ui_free_modules_do_not_import_streamlit(module):
    tree = ast.parse((DASH / f"{module}.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "streamlit" not in imported


@pytest.mark.parametrize(
    "page", [p for p in PAGES if p.name not in {"scenario_lab.py", "__init__.py"}]
)
def test_pages_do_not_recompute_pricing_intelligence(page):
    text = page.read_text(encoding="utf-8")
    offenders = [token for token in RECOMPUTATION_TOKENS if token in text]
    assert offenders == [], f"{page.name} recomputes backend logic: {offenders}"


def test_only_the_operations_page_can_reach_actions():
    importers = sorted(
        p.name
        for p in PAGES
        if "dashboard import actions" in p.read_text(encoding="utf-8")
        or "dashboard.actions" in p.read_text(encoding="utf-8")
    )
    assert importers == ["operations.py"]
    for module in ("services", "formatting", "charts", "models", "results", "scenario_text"):
        text = (DASH / f"{module}.py").read_text(encoding="utf-8")
        assert "dashboard.actions" not in text and "import actions" not in text


def _attribute_names(node: ast.AST) -> set[str]:
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


@pytest.mark.parametrize("page", [p for p in PAGES if p.name != "__init__.py"])
def test_untrusted_text_never_reaches_a_markdown_surface(page):
    """``st.dataframe`` cells and ``st.link_button`` URLs are literal; Markdown is not."""
    tree = ast.parse(page.read_text(encoding="utf-8"))
    offenders = []
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr in MARKDOWN_SURFACES):
            continue
        used = set()
        for arg in (*call.args, *(kw.value for kw in call.keywords)):
            used |= _attribute_names(arg)
        bad = used & UNTRUSTED_ATTRIBUTES
        if bad:
            offenders.append((call.lineno, func.attr, sorted(bad)))
    assert offenders == [], f"{page.name}: untrusted fields rendered via Markdown: {offenders}"


def test_the_untrusted_text_check_can_actually_fail():
    """Guard the guard: a page that interpolates a scraped field into st.markdown is caught."""
    tree = ast.parse('st.markdown(f"- {detail.product_name}")')
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert _attribute_names(call.args[0]) & UNTRUSTED_ATTRIBUTES == {"product_name"}


def test_market_intelligence_does_not_depend_on_the_dashboard():
    offenders = [
        p.name
        for p in (SRC / "pricing_intelligence").rglob("*.py")
        if "balance_pricing_engine.dashboard" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_router_registers_all_seven_pages_and_no_stubs_remain():
    router = (DASH / "app.py").read_text(encoding="utf-8")
    for title in (
        "Overview",
        "Market Watch",
        "Rate Changes",
        "Historical Trends",
        "Pricing Scenarios",
        "Operations",
        "System Health",
    ):
        assert f'"{title}"' in router
    assert not [p.name for p in PAGES if "Coming in a later task" in p.read_text(encoding="utf-8")]
