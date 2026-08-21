"""Model validation report drafting - decision-support only, requires human review."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DECISION_SUPPORT_TAG = "[DECISION-SUPPORT: requires human review]"
_DEFAULT_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = (
    "You are a model risk reviewer assisting a retail bank's pricing team. "
    "Your role is to draft a validation report explaining a candidate response model's "
    "evaluation results in plain language for a human reviewer who must approve or reject it. "
    "You MUST NOT approve the model, promote it to champion, or state that it is safe to deploy. "
    "Every response must end with: " + _DECISION_SUPPORT_TAG
)


@dataclass(frozen=True)
class ValidationReportResult:
    narrative_text: str
    used_llm: bool
    model_used: str
    prompt_hash: str
    timestamp_utc: str


class ModelValidationReportAgent:
    """Draft a decision-support validation report for a ModelRecord."""

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

    def draft_report(self, record: Any) -> ValidationReportResult:
        """Draft the validation report. Falls back to structured text if LLM unavailable."""
        user_prompt = _build_user_prompt(record)
        prompt_hash = _hash_prompt(user_prompt)
        timestamp = datetime.now(timezone.utc).isoformat()

        narrative, used_llm, model_used = self._generate(user_prompt)
        # Must check the marker is the *final* text, not merely present anywhere: the
        # system prompt requires every response to end with it, and an LLM response that
        # contains it mid-text (e.g. followed by an actionable conclusion) must still get
        # the marker appended -- a bare substring check would leave that conclusion
        # outside the human-review warning.
        tagged = (
            narrative
            if narrative.rstrip().endswith(_DECISION_SUPPORT_TAG)
            else narrative + f"\n\n{_DECISION_SUPPORT_TAG}"
        )

        result = ValidationReportResult(
            narrative_text=tagged,
            used_llm=used_llm,
            model_used=model_used,
            prompt_hash=prompt_hash,
            timestamp_utc=timestamp,
        )
        self._write_audit_log(result)
        return result

    def _generate(self, user_prompt: str) -> tuple[str, bool, str]:
        if not self._use_llm:
            return _fallback_report(user_prompt), False, "fallback"
        client = self._client
        if client is None:
            try:
                from balance_pricing_engine.llm._client import build_client

                client = build_client()
            except ImportError:
                return _fallback_report(user_prompt), False, "fallback"
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Failed to build LLM client; using fallback report: %s",
                    exc,
                )
                return _fallback_report(user_prompt), False, "fallback"
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
            return _fallback_report(user_prompt), False, "fallback"

    def _write_audit_log(self, result: ValidationReportResult) -> None:
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


def _build_user_prompt(record: Any) -> str:
    metrics = record.metrics or {}
    features_line = ", ".join(record.feature_columns) if record.feature_columns else "unknown"
    lines = [
        "Please draft a validation report for the following candidate response model, "
        "for a human reviewer who must decide whether to approve it:",
        "",
        f"Model name: {record.model_name}",
        f"Model type: {record.model_type}",
        f"Version: {record.version}",
        f"Trained at (UTC): {record.trained_at_utc}",
        f"Config hash: {record.config_hash}",
        f"Features: {features_line}",
        f"Target: {record.target_column or 'unknown'}",
        f"AUC: {metrics.get('auc', float('nan')):.4f}",
        f"Log loss: {metrics.get('log_loss', float('nan')):.4f}",
        f"Brier score: {metrics.get('brier_score', float('nan')):.4f}",
        f"Calibration MAE: {record.calibration_mae:.4f}",
        f"Governance status: {record.governance_status}",
        f"Champion status: {record.champion_status}",
        "",
        "Explain what these results mean for model risk, flag any calibration or "
        "performance concerns, and summarise what a reviewer should check before sign-off.",
    ]
    return "\n".join(lines)


def _fallback_report(user_prompt: str) -> str:
    lines = user_prompt.split("\n")
    data_lines = [
        line
        for line in lines
        if ":" in line and not line.startswith("Please") and not line.startswith("Explain")
    ]
    summary = "\n".join(f"  - {line.strip()}" for line in data_lines if line.strip())
    return (
        "## Model Validation Report\n\n"
        "The following evaluation results were produced for this candidate model:\n\n"
        f"{summary}\n\n"
        "Reviewer checklist:\n"
        "- Verify calibration MAE is within acceptable tolerance for this model type.\n"
        "- Confirm AUC, log loss, and Brier score are consistent with the champion "
        "or with prior challenger runs.\n"
        "- Check governance_status and champion_status before approving or promoting.\n"
        "- Validate feature and target definitions match the intended use case.\n\n"
        f"{_DECISION_SUPPORT_TAG}"
    )


def _hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
