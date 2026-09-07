import pytest

from balance_pricing_engine.competitor_rates.normalise import (
    normalise_balance,
    normalise_frequency,
    normalise_lvr_condition,
    normalise_lvr_values,
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
        ("Annual Interest Rate", "unknown"),
        ("Annual Rate", "unknown"),
        ("Yearly Rate", "unknown"),
        ("Annual Percentage Rate", "unknown"),
        ("Annualised Rate", "unknown"),
        ("Annualized Rate", "unknown"),
        ("Annual Interest Rate | Monthly", "monthly"),
        ("Annual Percentage Rate | Monthly", "monthly"),
        ("Annual Percentage Yield", "unknown"),
        ("Annual Return", "unknown"),
        ("Annual Percentage Yield | Monthly", "monthly"),
    ],
)
def test_normalise_frequency_maps_common_labels(label, expected):
    assert normalise_frequency(label) == expected


def test_normalise_frequency_annual_rate_label_does_not_suppress_a_genuine_annual_payment_mention():
    """A deposit table's "Annual Rate" column header (describing the rate's
    own annualised unit) and a genuine "paid annually" payment-frequency
    mention can coexist in the same flattened raw_text_context -- stripping
    only the rate-label phrase must not also erase the separate, real
    payment-frequency mention it sits alongside."""
    assert normalise_frequency("Annual Rate | Paid: Monthly or Annually") == "annually+monthly"


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


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("not less than 20% equity", "min_20_percent_equity"),
        ("unavailable for LVR above 80%", "standard"),
        ("does not apply to LVR above 80%", "standard"),
        ("no longer available for LVR above 80%", "standard"),
        ("excludes LVR above 80%", "standard"),
    ],
)
def test_normalise_lvr_condition_respects_negation_before_a_low_equity_phrase(label, expected):
    """A negation immediately before a low-equity phrase reverses its
    meaning -- "not less than 20% equity" states a minimum was met
    (standard), not missed (low-equity). Without this, a wording-only edit
    from "at least 20% equity" to "not less than 20% equity" would flip
    rate_category/lvr_condition and read as a genuine eligibility change."""
    assert normalise_lvr_condition(label) == expected


def test_normalise_lvr_values_of_a_genuine_low_equity_phrase_is_unknown():
    """max_lvr/min_equity express upper LVR/lower equity bounds only -- a
    low-equity upper bound would invert their meaning if stored, so a
    genuine low-equity phrase must resolve to (None, None), not fabricate a
    reversed bound."""
    assert normalise_lvr_values("less than 20% equity") == (None, None)
    assert normalise_lvr_values("LVR above 80%") == (None, None)


def test_normalise_lvr_values_respects_negation_before_a_low_equity_phrase():
    """The same negation-blindness bug applies here: "not less than 20%
    equity" states a genuine 20%-equity minimum, so it must resolve to the
    normal (max_lvr, min_equity) pair instead of being discarded as
    low-equity."""
    assert normalise_lvr_values("not less than 20% equity") == (0.8, 0.2)


def test_normalise_lvr_condition_checks_every_low_equity_match_not_just_the_first():
    """A disclosure can combine a negated restriction with a genuine
    low-equity condition in a later clause: "not available for LVR above
    90%. LVR above 80% attracts a margin" negates the first match but the
    second clause is a real low-equity condition. Stopping at the first
    (negated) match would read the whole disclosure as "standard" and let a
    later wording change produce a false eligibility event."""
    label = "not available for LVR above 90%. LVR above 80% attracts a margin"

    assert normalise_lvr_condition(label) == "low_equity_margin_applies"


def test_normalise_lvr_values_of_a_negated_clause_survives_a_separate_low_equity_clause():
    """"not less than 90% equity" (negated) states a genuine met minimum --
    at least 90% equity -- independent of the second clause's own, separate
    low-equity condition. Only the genuinely low-equity clause is masked
    out; the first clause's own hard bound must still resolve, the same
    principle as "Maximum 90% LVR. LVR above 80% attracts a margin"."""
    label = "not less than 90% equity. less than 20% equity applies to new lending"

    assert normalise_lvr_values(label) == (0.1, 0.9)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Not available to trusts, but LVR above 80% attracts a margin", "low_equity_margin_applies"),
        ("not less than 20%, 25% or 30% equity", "min_20_percent_equity"),
    ],
)
def test_normalise_lvr_condition_scopes_negation_to_the_clause_not_the_whole_string(
    label, expected
):
    """A full stop/semicolon always starts a new clause, but a plain comma
    does not: "Not available to trusts, but LVR above 80%..." joins an
    unrelated negated clause to a genuine restriction with a comma, and the
    leading "not" must not reach across it -- while "not less than 20%, 25%
    or 30% equity" is a single negated clause (a list), where treating the
    comma as a boundary would wrongly split the negation from its own
    phrase."""
    assert normalise_lvr_condition(label) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Loans with LVR above 80% are unavailable", "standard"),
        ("LVR above 80% is unavailable", "standard"),
    ],
)
def test_normalise_lvr_condition_respects_negation_placed_after_the_phrase(label, expected):
    """A denial can follow the matched condition instead of preceding it:
    "LVR above 80% are unavailable" states the tier is explicitly
    ineligible, the opposite of a genuine low-equity disclosure. Checking
    only the text before the match would miss this and label an explicitly
    excluded tier as low-equity."""
    assert normalise_lvr_condition(label) == expected


def test_normalise_lvr_values_respects_negation_placed_after_the_phrase():
    """normalise_lvr_values must likewise check for a negation that follows
    the match: "less than 20% equity is not required" states the low-equity
    condition does not apply, so the genuine 20%-equity bound must be
    extracted via the normal fallback, not discarded as if this were an
    unqualified low-equity disclosure."""
    assert normalise_lvr_values("less than 20% equity is not required") == (0.8, 0.2)


def test_normalise_lvr_values_preserves_a_hard_cap_alongside_a_separate_low_equity_clause():
    """A disclosure can combine an explicit eligibility cap with a genuine
    low-equity threshold in a separate clause: "Maximum 90% LVR. LVR above
    80% attracts a low-equity margin" states a real 90% LVR ceiling that
    must survive, not be discarded just because a later clause also
    describes a genuine low-equity condition -- only that clause is masked
    out of the numeric search, not the whole string."""
    label = "Maximum 90% LVR. LVR above 80% attracts a low-equity margin"

    assert normalise_lvr_values(label) == (0.9, 0.1)


def test_normalise_lvr_values_preserves_a_hard_cap_joined_by_a_bare_conjunction():
    """The same principle applies when the two clauses are joined by a bare
    conjunction rather than a full stop: "Maximum 90% LVR and LVR above 80%
    attracts a low-equity margin" has no comma or period before "and", but
    the repeated "LVR" still marks a genuine second clause -- the 90% cap
    must not be discarded just because the boundary lacks punctuation."""
    label = "Maximum 90% LVR and LVR above 80% attracts a low-equity margin"

    assert normalise_lvr_values(label) == (0.9, 0.1)


def test_normalise_lvr_condition_does_not_split_a_clause_at_a_decimal_point():
    """A "." inside an advertised decimal rate is not a sentence boundary:
    "Not available at 4.99% for LVR above 80%" must keep "Not available" in
    the same clause as the LVR match, or the decimal point would silently
    cut the negation away from the condition it negates."""
    assert normalise_lvr_condition("Not available at 4.99% for LVR above 80%") == "standard"


def test_normalise_lvr_condition_a_bare_or_between_rate_options_is_not_a_boundary():
    """A bare "or" joining two rate options for *one* condition must not
    become a clause boundary just because a later fix taught boundary
    detection to recognise a bare conjunction: "LVR above 80% or 85% is
    unavailable" states one denial covering both options, and splitting at
    "or" would cut the trailing negation out of the clause it belongs to."""
    assert normalise_lvr_condition("LVR above 80% or 85% is unavailable") == "standard"


def test_normalise_balance_extracts_currency_amounts():
    assert normalise_balance("$5,000+") == (5000.0, None)
    assert normalise_balance("$2,000 to $5,000,000") == (2000.0, 5_000_000.0)
    assert normalise_balance("no minimum shown") == (None, None)


def test_normalise_balance_ascending_unrelated_minimums_do_not_become_a_range():
    """Two unrelated minimums (a product-wide minimum plus a row-specific
    one) can happen to ascend in document order without being a range --
    max_balance_or_deposit is identity-bearing, so inventing one from
    numeric order alone would let either boilerplate minimum changing turn
    an unchanged advertised rate into false new/removed product events.
    Only an explicit range separator directly between the two amounts
    (a "to"/hyphen, nothing else in between) confirms a genuine range."""
    assert normalise_balance("Minimum balance $1,000 ... Minimum $5,000") == (1000.0, None)


def test_normalise_balance_hyphen_separator_is_a_genuine_range():
    assert normalise_balance("$10,000-$49,999") == (10000.0, 49999.0)


def test_normalise_balance_an_unrelated_leading_minimum_before_a_genuine_range_is_not_folded_in():
    """A genuine range separator existing *somewhere* in the text is not
    enough -- pairing the first two amounts found regardless of where the
    separator actually sits would fold an unrelated leading minimum into a
    range it was never part of, fabricating a maximum. The leading minimum
    must be returned alone; the genuine range's own two amounts (5000,
    9999) belong to a different figure and must not surface as the max
    either."""
    assert normalise_balance("Minimum $1,000 | Balance $5,000 to $9,999") == (1000.0, None)


@pytest.mark.parametrize(
    ("label", "expected_months"),
    [
        ("Fixed - 1 year", 12),
        ("Standard - 18 months", 18),
        ("Fixed rate - 5 years", 60),
        ("Home loan - 6 months", 6),
        ("6-12 months", 12),
    ],
)
def test_normalise_term_keeps_a_dash_separated_label_a_real_term(label, expected_months):
    """A dash used as a *separator* is not a minus sign.

    The malformed-duration guard rejects a negative duration ("-1 year"), but
    a bank labelling a row "Fixed - 1 year" is stating a normal 12-month term.
    Treating the separator as a sign drops the row's fixed term entirely, so
    the product silently leaves the fixed-term market comparison -- the same
    class of silent-drop defect as a mis-set default_fixed_category.
    """
    assert normalise_term(label).months == expected_months


@pytest.mark.parametrize("label", ["-1 year", "-6 months", "-30 days"])
def test_normalise_term_still_rejects_a_signed_duration(label):
    """The sign attached directly to the duration still invalidates it."""
    assert normalise_term(label).months is None
