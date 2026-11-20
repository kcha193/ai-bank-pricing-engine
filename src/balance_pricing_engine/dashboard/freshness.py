"""Is a stored Pricing Studio result still safe to show?

One Streamlit-free rule, so the page never scatters its own freshness checks. A result is kept
in session state as ``(request, result, db_stamp, computed_at, config_fingerprint)``; it is
*current* only when every one of those still matches what the page would compute now. Anything
else is hidden (never greyed out, never shown beside a caption) until the operator clicks
"Run scenario". Hidden results stay in session state: if the inputs return to what was run,
the same result is current again, with no recomputation.

Evaluation order is most specific cause first, so the message names the real reason: a term
that disappeared because new data arrived is reported as an unavailable term, not as a generic
data change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from balance_pricing_engine.dashboard.models import ScenarioRequest
from balance_pricing_engine.pricing_intelligence.revision import DatabaseStamp

_RERUN = "Click Run scenario to calculate a new result."


class ResultFreshness(Enum):
    CURRENT = "current"
    INPUTS_CHANGED = "inputs_changed"
    INPUT_UNAVAILABLE = "input_unavailable"
    MARKET_DATA_CHANGED = "market_data_changed"
    CONFIG_CHANGED = "config_changed"
    TERM_UNAVAILABLE = "term_unavailable"
    LEGACY_RESULT = "legacy_result"
    WRONG_MODE = "wrong_mode"


_MESSAGES = {
    ResultFreshness.INPUTS_CHANGED: (
        "Scenario inputs have changed since this result was computed. "
        f"The previous modelled result is hidden. {_RERUN}"
    ),
    ResultFreshness.INPUT_UNAVAILABLE: (
        "Scenario assumptions or market data have changed, and the current inputs cannot form a "
        "valid scenario. Previous modelled results are hidden. Once the inputs are available "
        "again, click Run scenario to calculate a new result."
    ),
    ResultFreshness.MARKET_DATA_CHANGED: (
        "Market data has changed since this result was computed. "
        f"The previous modelled result is hidden. {_RERUN}"
    ),
    ResultFreshness.CONFIG_CHANGED: (
        "Engine configuration has changed since this result was computed. "
        f"The previous modelled result is hidden. {_RERUN}"
    ),
    ResultFreshness.TERM_UNAVAILABLE: (
        "The result's market term is no longer available. "
        f"The previous modelled result is hidden. {_RERUN}"
    ),
    ResultFreshness.LEGACY_RESULT: (
        "This stored result has no record of the engine configuration it was computed with. "
        f"The previous modelled result is hidden. {_RERUN}"
    ),
    ResultFreshness.WRONG_MODE: (
        "The stored result belongs to a different scenario type. "
        f"Previous modelled results are hidden. {_RERUN}"
    ),
}


@dataclass(frozen=True)
class FreshnessOutcome:
    status: ResultFreshness
    message: str = ""

    @property
    def is_current(self) -> bool:
        return self.status is ResultFreshness.CURRENT


def stored_request(stored: Any) -> Optional[ScenarioRequest]:
    """The request a stored entry was computed from, or ``None`` for anything malformed."""
    if isinstance(stored, tuple) and stored and isinstance(stored[0], ScenarioRequest):
        return stored[0]
    return None


def stored_computed_at(stored: Any) -> Optional[datetime]:
    """When a stored entry was computed, or ``None`` if the entry does not carry a timestamp."""
    if isinstance(stored, tuple) and len(stored) > 3 and isinstance(stored[3], datetime):
        return stored[3]
    return None


def _outcome(status: ResultFreshness) -> FreshnessOutcome:
    return FreshnessOutcome(status, _MESSAGES.get(status, ""))


def assess_result(
    stored: Any,
    *,
    mode: str,
    request: Optional[ScenarioRequest],
    db_stamp: Any,
    config_fingerprint: str,
    terms_available: bool,
) -> Optional[FreshnessOutcome]:
    """Whether ``stored`` may be rendered for the page's current state.

    ``None`` means nothing is stored. ``request`` is ``None`` when the current inputs cannot
    form a valid scenario (a required market term is not offered). ``terms_available`` says
    whether every market term the *stored* request was read from is still live.
    """
    if stored is None:
        return None
    original = stored_request(stored)
    # Session state written before configuration provenance existed (4 fields) is unprovable.
    if original is None or len(stored) < 5 or stored[4] is None:
        return _outcome(ResultFreshness.LEGACY_RESULT)
    if original.mode != mode:
        return _outcome(ResultFreshness.WRONG_MODE)
    if stored[4] != config_fingerprint:
        return _outcome(ResultFreshness.CONFIG_CHANGED)
    if not terms_available:
        return _outcome(ResultFreshness.TERM_UNAVAILABLE)
    if (
        not isinstance(db_stamp, DatabaseStamp)
        or not isinstance(stored[2], DatabaseStamp)
        or db_stamp.format_version != 1
        or stored[2] != db_stamp
    ):
        return _outcome(ResultFreshness.MARKET_DATA_CHANGED)
    if request is None:
        return _outcome(ResultFreshness.INPUT_UNAVAILABLE)
    if original != request:
        return _outcome(ResultFreshness.INPUTS_CHANGED)
    return FreshnessOutcome(ResultFreshness.CURRENT)
