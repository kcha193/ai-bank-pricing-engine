"""Scenario explanation narration - decision-support only, requires human review."""
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
    "Every response must end with: " + _DECISION_SUPPORT_TAG
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
        tagged = (
            narrative
            if _DECISION_SUPPORT_TAG in narrative
            else narrative + f"\n\n{_DECISION_SUPPORT_TAG}"
        )

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
            except Exception:
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
    data_lines = [
        line
        for line in lines
        if ":" in line and not line.startswith("Please") and not line.startswith("Explain")
    ]
    summary = "\n".join(f"  - {line.strip()}" for line in data_lines if line.strip())
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
