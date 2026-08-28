import pytest

from balance_pricing_engine.competitor_rates.normalise import (
    normalise_balance,
    normalise_frequency,
    normalise_lvr_condition,
    normalise_term,
)


@pytest.mark.parametrize(
    ("label", "months", "days", "special"),
    [
        ("6 months", 6, None, None),
        ("six months", 6, None, None),
        ("1 year", 12, None, None),
        ("18 months", 18, None, None),
        ("2 years", 24, None, None),
        ("30 days", 1, 30, None),
        ("60 days", 2, 60, None),
        ("90 days", 3, 90, None),
        ("floating", None, None, "floating"),
        ("variable", None, None, "variable"),
    ],
)
def test_normalise_term_preserves_raw_label_and_derives_term_fields(label, months, days, special):
    term = normalise_term(label)

    assert term.raw_label == label
    assert term.months == months
    assert term.days == days
    assert term.special_label == special


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Interest paid at maturity", "at_maturity"),
        ("Monthly interest", "monthly"),
        ("Quarterly", "quarterly"),
        ("Annual interest", "annually"),
        ("something bespoke", "unknown"),
    ],
)
def test_normalise_frequency_maps_common_labels(label, expected):
    assert normalise_frequency(label) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Monthly or at maturity", "at_maturity+monthly"),
        ("Quarterly or at maturity", "at_maturity+quarterly"),
        ("Monthly or quarterly", "monthly+quarterly"),
        ("At maturity or monthly", "at_maturity+monthly"),
    ],
)
def test_normalise_frequency_combines_a_genuine_multi_option_choice(label, expected):
    """interest_payment_frequency is identity-bearing, and a deposit row's
    product_name is often a fixed generic string with no other field to
    distinguish it -- confidently picking one keyword out of a genuine
    multi-option label (e.g. always preferring "at maturity" over "monthly"),
    or collapsing every combination to plain "unknown", would both collapse
    two rows offering different option sets onto the same identity, and a
    rate difference between them would then read as a conflicting
    observation and reject both. The combined value is sorted so it does not
    depend on which order the source page lists the options in."""
    assert normalise_frequency(label) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("minimum 20% equity required", "min_20_percent_equity"),
        ("Special LVR under 80%", "min_20_percent_equity"),
        ("Special LVR < 80%", "min_20_percent_equity"),
        ("low equity margin applies", "low_equity_margin_applies"),
        ("standard rate", "standard"),
    ],
)
def test_normalise_lvr_condition_maps_common_mortgage_conditions(label, expected):
    assert normalise_lvr_condition(label) == expected


def test_normalise_balance_extracts_currency_amounts():
    assert normalise_balance("$5,000+") == (5000.0, None)
    assert normalise_balance("$2,000 to $5,000,000") == (2000.0, 5_000_000.0)
    assert normalise_balance("no minimum shown") == (None, None)
