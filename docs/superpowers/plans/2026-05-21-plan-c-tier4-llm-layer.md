# Plan C: Tier 4 LLM Layer — Scenario Narration & Config Anomaly Detection

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the two "Safe now" Tier 4 LLM use cases from the roadmap: (1) rule-backed config anomaly detection with optional LLM commentary, and (2) scenario explanation narration via the Anthropic API. Both have deterministic fallbacks, full audit logs, and are tagged as decision-support only.

**Architecture:** A new `src/balance_pricing_engine/llm/` module contains three files: `_client.py` (lazy Anthropic init), `anomaly.py` (`ConfigAnomalyChecker` — pure rules, no LLM), and `narrative.py` (`ScenarioNarrativeAgent` — uses Claude API if available, falls back to a structured text summary). Outputs are tagged `[DECISION-SUPPORT: requires human review]`. All API calls are logged to a JSON audit file. A new `[llm]` optional extra adds `anthropic>=0.30`.

**Governance constraints (from roadmap):**
- No autonomous model promotion, deployment, or price-setting.
- Human review required on all outputs.
- Prompt hashes recorded in audit log for reproducibility.
- Deterministic fallback always available.
- Outputs tagged as decision-support narrative only.

**Tech Stack:** Python 3.9+, `anthropic>=0.30` (optional `[llm]` extra), `claude-sonnet-4-6` as default model, pytest with monkeypatched Anthropic client for tests (no real API calls).

---

## File Map

| Action | Path | Responsibility |
|--------|------|---------------|
| Modify | `pyproject.toml` | Add `[llm]` optional extra |
| Create | `src/balance_pricing_engine/llm/__init__.py` | Re-exports `ConfigAnomalyChecker`, `ScenarioNarrativeAgent` |
| Create | `src/balance_pricing_engine/llm/_client.py` | Lazy Anthropic SDK import, `build_client()` |
| Create | `src/balance_pricing_engine/llm/anomaly.py` | `AnomalyReport` dataclass, `ConfigAnomalyChecker`, rule implementations |
| Create | `src/balance_pricing_engine/llm/narrative.py` | `ScenarioNarrativeAgent`, structured fallback, audit logger |
| Create | `tests/unit/test_llm_anomaly.py` | Tests for all anomaly rules (no API calls) |
| Create | `tests/unit/test_llm_narrative.py` | Tests for narration with monkeypatched client |
| Create | `examples/run_llm_diagnostics.py` | End-to-end example combining anomaly + narrative |

---

## Task 1: Add `[llm]` optional extra to `pyproject.toml`

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the extra**

In `pyproject.toml`, find the `[project.optional-dependencies]` section and add one line:

Old (last line before `[tool.pytest.ini_options]`):
```toml
dashboard = ["streamlit>=1.30", "matplotlib>=3.8"]
```

New:
```toml
dashboard = ["streamlit>=1.30", "matplotlib>=3.8"]
llm = ["anthropic>=0.30"]
```

- [ ] **Step 2: Verify the project is still installable**

```bash
python3 -m pip install -e ".[dev]" --quiet
```

Expected: installs without error. `anthropic` is NOT installed (it's optional).

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "build: add [llm] optional extra for anthropic SDK"
```

---

## Task 2: Create `llm/_client.py` — lazy Anthropic client

**Files:**
- Create: `src/balance_pricing_engine/llm/__init__.py`
- Create: `src/balance_pricing_engine/llm/_client.py`
- Create: `tests/unit/test_llm_anomaly.py` (empty for now, populated in Task 3)

- [ ] **Step 1: Create the module**

Create `src/balance_pricing_engine/llm/__init__.py`:

```python
"""Tier 4 LLM-assisted diagnostics layer. Requires the [llm] optional extra."""
from balance_pricing_engine.llm.anomaly import AnomalyReport, ConfigAnomalyChecker
from balance_pricing_engine.llm.narrative import ScenarioNarrativeAgent

__all__ = ["AnomalyReport", "ConfigAnomalyChecker", "ScenarioNarrativeAgent"]
```

Create `src/balance_pricing_engine/llm/_client.py`:

```python
"""Lazy Anthropic SDK loader — importable without the SDK installed."""
from __future__ import annotations

from typing import Any


def build_client(api_key: str | None = None) -> Any:
    """Return an Anthropic client, or raise ImportError with install hint."""
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError(
            "The 'anthropic' package is required for LLM features. "
            "Install it with: pip install 'balance_pricing_engine[llm]'"
        ) from exc
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def is_llm_available() -> bool:
    """Return True if the anthropic SDK is installed."""
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False
```

- [ ] **Step 2: Verify imports work without `anthropic` installed**

```bash
python3 -c "from balance_pricing_engine.llm._client import is_llm_available; print(is_llm_available())"
```

Expected: prints `False` (since `anthropic` is not yet installed).

- [ ] **Step 3: Commit**

```bash
git add src/balance_pricing_engine/llm/
git commit -m "feat: add llm module skeleton with lazy Anthropic client"
```

---

## Task 3: `ConfigAnomalyChecker` — rule-backed anomaly detection

**Files:**
- Create: `src/balance_pricing_engine/llm/anomaly.py`
- Create: `tests/unit/test_llm_anomaly.py`

The roadmap marks this as "Safe now (rule-backed checks)" — no LLM required for the core checker.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_llm_anomaly.py`:

```python
import pytest
from balance_pricing_engine.config import load_config
from balance_pricing_engine.llm.anomaly import AnomalyReport, ConfigAnomalyChecker


def _base_config():
    return load_config("configs/base.yaml")


def test_clean_config_has_no_anomalies():
    config = _base_config()
    checker = ConfigAnomalyChecker()
    reports = checker.check(config)
    assert reports == [], f"Expected no anomalies, got: {reports}"


def test_negative_wholesale_spread_flagged():
    config = _base_config().model_copy(
        update={"market": _base_config().market.model_copy(
            update={"wholesale_funding_spread": -0.005}
        )}
    )
    reports = checker_check(config)
    assert any(r.rule_id == "negative_wholesale_spread" for r in reports)
    assert all(r.severity in ("warning", "error") for r in reports)


def checker_check(config):
    return ConfigAnomalyChecker().check(config)


def test_penalty_dominated_by_funding_spread_flagged():
    config = _base_config().model_copy(
        update={
            "market": _base_config().market.model_copy(
                update={"wholesale_funding_spread": 0.05}
            ),
            "balance_sheet": _base_config().balance_sheet.model_copy(
                update={"funding_shortfall_penalty_rate": 0.001}
            ),
        }
    )
    reports = checker_check(config)
    assert any(r.rule_id == "penalty_dominated_by_funding_spread" for r in reports)


def test_small_customer_pool_flagged():
    config = _base_config().model_copy(
        update={"simulation": _base_config().simulation.model_copy(
            update={"n_loan_customers": 500}
        )}
    )
    reports = checker_check(config)
    assert any(r.rule_id == "small_customer_pool" for r in reports)


def test_narrow_spread_band_flagged():
    from balance_pricing_engine.config import SpreadGridConfigModel
    config = _base_config().model_copy(
        update={"pricing_strategy": _base_config().pricing_strategy.model_copy(
            update={
                "home_loan_spread_bps": SpreadGridConfigModel(min=-20, max=20, step=10),
            }
        )}
    )
    reports = checker_check(config)
    assert any(r.rule_id == "narrow_spread_band" for r in reports)


def test_anomaly_report_has_required_fields():
    config = _base_config().model_copy(
        update={"market": _base_config().market.model_copy(
            update={"wholesale_funding_spread": -0.005}
        )}
    )
    reports = checker_check(config)
    negative_report = next(r for r in reports if r.rule_id == "negative_wholesale_spread")
    assert negative_report.severity in ("warning", "error")
    assert negative_report.message
    assert negative_report.field_path
    assert negative_report.observed_value is not None


def test_competitor_rates_enabled_but_strict_false_flagged():
    config = _base_config().model_copy(
        update={"competitor_rates": _base_config().competitor_rates.model_copy(
            update={"enabled": True, "strict": False}
        )}
    )
    reports = checker_check(config)
    assert any(r.rule_id == "competitor_rates_non_strict" for r in reports)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/test_llm_anomaly.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'balance_pricing_engine.llm.anomaly'`

- [ ] **Step 3: Create `anomaly.py`**

Create `src/balance_pricing_engine/llm/anomaly.py`:

```python
"""Rule-backed config anomaly detection. No LLM required."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AnomalyReport:
    rule_id: str
    severity: str  # "warning" | "error"
    field_path: str
    observed_value: Any
    message: str


class ConfigAnomalyChecker:
    """Check an EngineConfig for known risky or inconsistent configurations."""

    def check(self, config: Any) -> list[AnomalyReport]:
        """Run all rules and return a list of AnomalyReports."""
        reports: list[AnomalyReport] = []
        reports.extend(self._check_negative_wholesale_spread(config))
        reports.extend(self._check_penalty_dominated(config))
        reports.extend(self._check_small_customer_pool(config))
        reports.extend(self._check_narrow_spread_band(config))
        reports.extend(self._check_competitor_rates_non_strict(config))
        return reports

    def _check_negative_wholesale_spread(self, config: Any) -> list[AnomalyReport]:
        spread = getattr(getattr(config, "market", None), "wholesale_funding_spread", None)
        if spread is not None and spread < 0:
            return [AnomalyReport(
                rule_id="negative_wholesale_spread",
                severity="warning",
                field_path="market.wholesale_funding_spread",
                observed_value=spread,
                message=(
                    f"wholesale_funding_spread={spread:.4f} is negative. "
                    "This means the engine is modelling a below-cash-rate funding benefit, "
                    "which is unusual. Verify this is intentional."
                ),
            )]
        return []

    def _check_penalty_dominated(self, config: Any) -> list[AnomalyReport]:
        spread = getattr(getattr(config, "market", None), "wholesale_funding_spread", None)
        penalty = getattr(getattr(config, "balance_sheet", None), "funding_shortfall_penalty_rate", None)
        if spread is not None and penalty is not None and spread > 0 and penalty < spread * 0.1:
            return [AnomalyReport(
                rule_id="penalty_dominated_by_funding_spread",
                severity="warning",
                field_path="balance_sheet.funding_shortfall_penalty_rate",
                observed_value=penalty,
                message=(
                    f"funding_shortfall_penalty_rate={penalty:.4f} is less than 10% of "
                    f"wholesale_funding_spread={spread:.4f}. The shortfall penalty may be too "
                    "weak to meaningfully steer deposit funding behaviour."
                ),
            )]
        return []

    def _check_small_customer_pool(self, config: Any) -> list[AnomalyReport]:
        reports: list[AnomalyReport] = []
        sim = getattr(config, "simulation", None)
        if sim is None:
            return []
        for field, attr in [("n_loan_customers", "loan"), ("n_deposit_customers", "deposit")]:
            n = getattr(sim, field, None)
            if n is not None and n < 1000:
                reports.append(AnomalyReport(
                    rule_id="small_customer_pool",
                    severity="warning",
                    field_path=f"simulation.{field}",
                    observed_value=n,
                    message=(
                        f"simulation.{field}={n} is below 1000. Small pools increase Monte Carlo "
                        f"variance in {attr} volume estimates and may destabilise sensitivity analysis."
                    ),
                ))
        return reports

    def _check_narrow_spread_band(self, config: Any) -> list[AnomalyReport]:
        reports: list[AnomalyReport] = []
        ps = getattr(config, "pricing_strategy", None)
        if ps is None:
            return []
        for field, label in [
            ("home_loan_spread_bps", "home loan"),
            ("term_deposit_spread_bps", "term deposit"),
        ]:
            band = getattr(ps, field, None)
            if band is None:
                continue
            width_bps = getattr(band, "max", 0) - getattr(band, "min", 0)
            if width_bps < 50:
                reports.append(AnomalyReport(
                    rule_id="narrow_spread_band",
                    severity="warning",
                    field_path=f"pricing_strategy.{field}",
                    observed_value=width_bps,
                    message=(
                        f"pricing_strategy.{field} band width={width_bps} bps is below 50 bps. "
                        f"A narrow {label} spread band risks hitting boundary limits and may "
                        "require boundary expansion."
                    ),
                ))
        return reports

    def _check_competitor_rates_non_strict(self, config: Any) -> list[AnomalyReport]:
        cr = getattr(config, "competitor_rates", None)
        if cr is None:
            return []
        if getattr(cr, "enabled", False) and not getattr(cr, "strict", True):
            return [AnomalyReport(
                rule_id="competitor_rates_non_strict",
                severity="warning",
                field_path="competitor_rates.strict",
                observed_value=False,
                message=(
                    "competitor_rates.enabled=True but strict=False. "
                    "Validation failures will warn and continue rather than abort. "
                    "Consider strict=True in production runs."
                ),
            )]
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/test_llm_anomaly.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/llm/anomaly.py tests/unit/test_llm_anomaly.py
git commit -m "feat: add ConfigAnomalyChecker with rule-backed config validation"
```

---

## Task 4: `ScenarioNarrativeAgent` — LLM narration with deterministic fallback

**Files:**
- Create: `src/balance_pricing_engine/llm/narrative.py`
- Create: `tests/unit/test_llm_narrative.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_llm_narrative.py`:

```python
import json
import pytest
from unittest.mock import MagicMock, patch

from balance_pricing_engine.config import load_config
from balance_pricing_engine.llm.narrative import ScenarioNarrativeAgent
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


def _diagnostics():
    return run_baseline_diagnostics(load_config("configs/base.yaml"))


def test_narrative_agent_fallback_returns_structured_summary():
    diag = _diagnostics()
    agent = ScenarioNarrativeAgent(use_llm=False)
    result = agent.narrate(diag)

    assert result.narrative_text
    assert "8." in result.narrative_text or "loan" in result.narrative_text.lower()
    assert result.used_llm is False
    assert result.model_used == "fallback"
    assert "[DECISION-SUPPORT: requires human review]" in result.narrative_text


def test_narrative_agent_fallback_contains_key_numbers():
    diag = _diagnostics()
    agent = ScenarioNarrativeAgent(use_llm=False)
    result = agent.narrate(diag)
    # Should mention the best loan rate as a percentage
    assert f"{diag.best_result.loan_rate:.2%}" in result.narrative_text or (
        f"{diag.best_result.loan_rate * 100:.2f}" in result.narrative_text
    )


def test_narrative_agent_with_mocked_llm_returns_llm_response():
    diag = _diagnostics()

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text="This is a mocked LLM narrative about pricing.")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message

    agent = ScenarioNarrativeAgent(use_llm=True, client=mock_client)
    result = agent.narrate(diag)

    assert result.used_llm is True
    assert "mocked LLM narrative" in result.narrative_text
    assert "[DECISION-SUPPORT: requires human review]" in result.narrative_text
    assert result.model_used == "claude-sonnet-4-6"
    mock_client.messages.create.assert_called_once()


def test_narrative_agent_falls_back_on_api_error():
    diag = _diagnostics()

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("API unavailable")

    agent = ScenarioNarrativeAgent(use_llm=True, client=mock_client)
    result = agent.narrate(diag)

    assert result.used_llm is False
    assert result.model_used == "fallback"
    assert result.narrative_text  # fallback text still present


def test_narrative_result_has_prompt_hash():
    diag = _diagnostics()
    agent = ScenarioNarrativeAgent(use_llm=False)
    result = agent.narrate(diag)
    assert result.prompt_hash  # non-empty string


def test_narrative_agent_writes_audit_log(tmp_path):
    diag = _diagnostics()
    agent = ScenarioNarrativeAgent(use_llm=False, audit_log_path=tmp_path / "audit.jsonl")
    agent.narrate(diag)

    log_lines = (tmp_path / "audit.jsonl").read_text().strip().split("\n")
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["used_llm"] is False
    assert "prompt_hash" in entry
    assert "timestamp_utc" in entry
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/test_llm_narrative.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'balance_pricing_engine.llm.narrative'`

- [ ] **Step 3: Create `narrative.py`**

Create `src/balance_pricing_engine/llm/narrative.py`:

```python
"""Scenario explanation narration — decision-support only, requires human review."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DECISION_SUPPORT_TAG = "[DECISION-SUPPORT: requires human review]"
_DEFAULT_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = (
    "You are a pricing analyst assistant for a retail bank. "
    "Your role is to explain pricing diagnostic results in plain language for a human reviewer. "
    "You MUST NOT recommend autonomous actions, approve models, or set prices. "
    "Every response must end with: "
    + _DECISION_SUPPORT_TAG
)


@dataclass(frozen=True)
class NarrativeResult:
    narrative_text: str
    used_llm: bool
    model_used: str
    prompt_hash: str
    timestamp_utc: str


class ScenarioNarrativeAgent:
    """Generate a decision-support narrative for ScenarioDiagnostics output."""

    def __init__(
        self,
        use_llm: bool = True,
        client: Any | None = None,
        model: str = _DEFAULT_MODEL,
        audit_log_path: Path | str | None = None,
        max_tokens: int = 800,
    ):
        self._use_llm = use_llm
        self._client = client
        self._model = model
        self._audit_log_path = Path(audit_log_path) if audit_log_path else None
        self._max_tokens = max_tokens

    def narrate(self, diagnostics: Any) -> NarrativeResult:
        """Narrate the scenario diagnostics. Falls back to structured text if LLM unavailable."""
        user_prompt = _build_user_prompt(diagnostics)
        prompt_hash = _hash_prompt(user_prompt)
        timestamp = datetime.now(timezone.utc).isoformat()

        narrative, used_llm, model_used = self._generate(user_prompt)
        tagged = narrative if _DECISION_SUPPORT_TAG in narrative else narrative + f"\n\n{_DECISION_SUPPORT_TAG}"

        result = NarrativeResult(
            narrative_text=tagged,
            used_llm=used_llm,
            model_used=model_used,
            prompt_hash=prompt_hash,
            timestamp_utc=timestamp,
        )
        self._write_audit_log(result, user_prompt)
        return result

    def _generate(self, user_prompt: str) -> tuple[str, bool, str]:
        if not self._use_llm:
            return _fallback_narrative(user_prompt), False, "fallback"
        client = self._client
        if client is None:
            try:
                from balance_pricing_engine.llm._client import build_client
                client = build_client()
            except ImportError:
                return _fallback_narrative(user_prompt), False, "fallback"
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = response.content[0].text
            return text, True, self._model
        except Exception:
            return _fallback_narrative(user_prompt), False, "fallback"

    def _write_audit_log(self, result: NarrativeResult, user_prompt: str) -> None:
        if self._audit_log_path is None:
            return
        self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp_utc": result.timestamp_utc,
            "used_llm": result.used_llm,
            "model_used": result.model_used,
            "prompt_hash": result.prompt_hash,
        }
        with open(self._audit_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def _build_user_prompt(diagnostics: Any) -> str:
    best = diagnostics.best_result
    bd = diagnostics.boundary_diagnostics
    lines = [
        "Please narrate the following pricing diagnostic results for a human pricing analyst:",
        "",
        f"Best loan rate: {best.loan_rate:.2%}",
        f"Best deposit rate: {best.deposit_rate:.2%}",
        f"Objective value: ${best.total_objective:,.0f}",
        f"Expected loan volume: ${best.expected_loan_volume:,.0f}",
        f"Expected deposit volume: ${best.expected_deposit_volume:,.0f}",
        f"Funding shortfall: ${best.funding_shortfall:,.0f}",
        f"Capital charge: ${best.capital_charge:,.0f}",
        f"Excess deposit income: ${best.excess_deposit_income:,.0f}",
        f"Boundary warning: {bd.has_warning}",
        f"Expansion rounds: {diagnostics.expansion_rounds}",
        f"Policy band breached: {diagnostics.policy_band_breached}",
        "",
        "Explain what these results mean for the bank's pricing decision, "
        "flag any concerns, and summarise what a reviewer should check before sign-off.",
    ]
    return "\n".join(lines)


def _fallback_narrative(user_prompt: str) -> str:
    lines = user_prompt.split("\n")
    data_lines = [l for l in lines if ":" in l and not l.startswith("Please") and not l.startswith("Explain")]
    summary = "\n".join(f"  - {l.strip()}" for l in data_lines if l.strip())
    return (
        "## Pricing Diagnostic Summary\n\n"
        "The following key metrics were produced by the pricing engine:\n\n"
        f"{summary}\n\n"
        "Reviewer checklist:\n"
        "- Verify boundary_warning and expansion_rounds are acceptable.\n"
        "- Confirm funding_shortfall is within risk appetite.\n"
        "- Check policy_band_breached flag before approving.\n"
        "- Validate rates against current competitor data.\n\n"
        f"{_DECISION_SUPPORT_TAG}"
    )


def _hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/test_llm_narrative.py -v
```

Expected: all 6 tests PASS. No real API calls are made.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/llm/narrative.py tests/unit/test_llm_narrative.py
git commit -m "feat: add ScenarioNarrativeAgent with LLM narration and deterministic fallback"
```

---

## Task 5: Update `llm/__init__.py` and verify full import chain

**Files:**
- Modify: `src/balance_pricing_engine/llm/__init__.py` (already created in Task 2)

- [ ] **Step 1: Verify the public API imports work**

```bash
python3 -c "
from balance_pricing_engine.llm import ConfigAnomalyChecker, ScenarioNarrativeAgent, AnomalyReport
print('imports ok:', ConfigAnomalyChecker, ScenarioNarrativeAgent, AnomalyReport)
"
```

Expected: prints the class names without error.

- [ ] **Step 2: Run full test suite**

```bash
python3 -m pytest -q
python3 -m ruff check .
```

Expected: all tests PASS, ruff clean.

- [ ] **Step 3: Commit if anything changed in `__init__.py`**

```bash
git add src/balance_pricing_engine/llm/__init__.py
git commit -m "feat: export ConfigAnomalyChecker and ScenarioNarrativeAgent from llm package"
```

---

## Task 6: Add `examples/run_llm_diagnostics.py`

**Files:**
- Create: `examples/run_llm_diagnostics.py`

- [ ] **Step 1: Create the example**

Create `examples/run_llm_diagnostics.py`:

```python
"""
Tier 4 LLM diagnostics example.

Runs config anomaly detection (rule-based, no API key needed) and
optional scenario narration (requires anthropic SDK + ANTHROPIC_API_KEY).

Usage:
    # Anomaly detection only (no API key needed):
    python3 examples/run_llm_diagnostics.py

    # With LLM narration (requires: pip install 'balance_pricing_engine[llm]'):
    ANTHROPIC_API_KEY=sk-ant-... python3 examples/run_llm_diagnostics.py --narrate
"""
from __future__ import annotations

import argparse
import sys

from balance_pricing_engine.config import load_config
from balance_pricing_engine.llm import AnomalyReport, ConfigAnomalyChecker, ScenarioNarrativeAgent
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


def main() -> None:
    parser = argparse.ArgumentParser(description="Tier 4 LLM diagnostics")
    parser.add_argument("--config", default="configs/base.yaml", help="Config file path")
    parser.add_argument(
        "--narrate",
        action="store_true",
        help="Generate scenario narration via Claude API (requires [llm] extra + API key)",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    print("=== Config Anomaly Check ===")
    checker = ConfigAnomalyChecker()
    reports: list[AnomalyReport] = checker.check(config)
    if not reports:
        print("  No anomalies detected.")
    else:
        for r in reports:
            print(f"  [{r.severity.upper()}] {r.rule_id}: {r.message}")
    print()

    if args.narrate:
        print("=== Scenario Narration (LLM) ===")
        diagnostics = run_baseline_diagnostics(config)
        from balance_pricing_engine.llm._client import is_llm_available
        use_llm = is_llm_available()
        if not use_llm:
            print("  anthropic SDK not installed — using deterministic fallback.")
        agent = ScenarioNarrativeAgent(use_llm=use_llm)
        result = agent.narrate(diagnostics)
        print(f"  Model used: {result.model_used}")
        print(f"  Prompt hash: {result.prompt_hash}")
        print()
        print(result.narrative_text)
    else:
        print("Tip: run with --narrate to generate a scenario narrative (requires [llm] extra).")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the example without `--narrate`**

```bash
python3 examples/run_llm_diagnostics.py
```

Expected: prints `No anomalies detected.` and the narration tip. No exception.

- [ ] **Step 3: Run the example with `--narrate` (no API key installed)**

```bash
python3 examples/run_llm_diagnostics.py --narrate
```

Expected: prints `anthropic SDK not installed — using deterministic fallback.` and then the fallback narrative text ending with `[DECISION-SUPPORT: requires human review]`. No exception.

- [ ] **Step 4: Commit**

```bash
git add examples/run_llm_diagnostics.py
git commit -m "feat: add run_llm_diagnostics example for Tier 4 anomaly detection and narration"
```

---

## Task 7: Open PR

- [ ] **Step 1: Push branch and open PR**

```bash
git push -u origin HEAD
gh pr create \
  --title "feat: Tier 4 LLM layer — config anomaly detection and scenario narration" \
  --body "$(cat <<'EOF'
## Summary
- New `balance_pricing_engine/llm/` module with `ConfigAnomalyChecker` (5 rule-backed checks, no API key needed) and `ScenarioNarrativeAgent` (Anthropic SDK with deterministic fallback)
- New `[llm]` optional extra (`pip install 'balance_pricing_engine[llm]'`) for Anthropic SDK
- All LLM outputs tagged `[DECISION-SUPPORT: requires human review]` — no autonomous actions
- Audit log records prompt hash, model, and timestamp for every narration call
- `examples/run_llm_diagnostics.py` demonstrates both features without needing an API key

## Governance
Complies with Tier 4 roadmap controls:
- No model promotion, deployment, or price-setting by LLM
- Human review required for all outputs
- Prompt hash captured in audit log for reproducibility
- Deterministic fallback always available (`use_llm=False` or SDK not installed)

## Test plan
- [ ] `test_llm_anomaly.py` (7 tests) — all rules, no API calls
- [ ] `test_llm_narrative.py` (6 tests) — fallback, mocked LLM, error fallback, audit log
- [ ] Example runs clean without API key
- [ ] All prior tests unchanged

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

**Spec coverage:**
- `[llm]` optional extra → Task 1 ✓
- Lazy Anthropic import (no hard dep) → Task 2 ✓
- `ConfigAnomalyChecker` with 5 rules → Task 3 ✓
- `ScenarioNarrativeAgent` with LLM + fallback + audit log + DECISION-SUPPORT tag → Task 4 ✓
- Prompt hash for reproducibility → Task 4 ✓
- Human review gate (tag in all outputs) → Task 4 ✓
- Example script → Task 6 ✓

**Placeholder scan:** None found.

**Type consistency:** `NarrativeResult` is `@dataclass(frozen=True)` throughout. `_build_user_prompt` accepts `Any` (duck-typed on `best_result`, `boundary_diagnostics`, `expansion_rounds`, `policy_band_breached`). Tests use `run_baseline_diagnostics` which returns a real `ScenarioDiagnostics` — no mock needed there.

**Governance check:** `_DECISION_SUPPORT_TAG` is a module-level constant used both in `_SYSTEM_PROMPT` and appended in `narrate()` if missing, so it appears in fallback and LLM outputs alike.
