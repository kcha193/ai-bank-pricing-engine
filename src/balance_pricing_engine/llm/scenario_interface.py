"""Natural language scenario-run interface. Decision-support only; requires human approval."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.optimization.response_fn import GovernanceError

_DECISION_SUPPORT_TAG = "[DECISION-SUPPORT: requires human review]"
_DEFAULT_MODEL = "claude-sonnet-4-6"

# Deliberate scenario assumptions only. Adding EngineConfig fields does not expand this list.
ALLOWED_PATCH_PATHS = frozenset(
    {
        ("random_seed",),
        ("market", "cash_rate"),
        ("market", "wholesale_funding_spread"),
        ("market", "competitor_loan_rate"),
        ("market", "competitor_deposit_rate"),
        ("market", "home_loan_reference_rate"),
        ("market", "term_deposit_reference_rate"),
        ("balance_sheet", "required_deposit_funding_ratio"),
        ("balance_sheet", "funding_shortfall_penalty_rate"),
        ("multiperiod", "loan_runoff_rate"),
        ("multiperiod", "deposit_runoff_rate"),
        ("multiperiod", "discount_rate"),
        ("multiperiod", "market_cash_rate_drift_bps"),
        ("multiperiod", "competitor_loan_rate_drift_bps"),
        ("multiperiod", "competitor_deposit_rate_drift_bps"),
        ("multiperiod", "reinvestment_spread_bps"),
        ("multiperiod", "loan_tenor_years"),
    }
)

_SYSTEM_PROMPT = (
    "You are a pricing configuration assistant. "
    "Given a natural language instruction and the current EngineConfig YAML, "
    "return ONLY a JSON object with a 'patches' list. "
    "Each patch must have 'path' (list of string keys) and 'value' (a finite JSON number). "
    "Only output the JSON - no explanation, no markdown. "
    "Only these paths are permitted: "
    + ", ".join(".".join(path) for path in sorted(ALLOWED_PATCH_PATHS))
    + '. Example: {"patches": [{"path": ["market", "cash_rate"], "value": 0.0475}]}'
)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


class ConfigPatch(BaseModel):
    """One approved numeric scenario field, never a section or operational setting."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    path: tuple[str, ...]
    value: int | float

    @field_validator("path")
    @classmethod
    def allowed_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value not in ALLOWED_PATCH_PATHS:
            raise ValueError("ConfigPatch path is not an allowed scenario assumption.")
        return value

    @field_validator("value", mode="before")
    @classmethod
    def numeric_value(cls, value: Any) -> Any:
        # No strings, booleans or objects: credentials cannot enter the accepted payload.
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("ConfigPatch value must be a finite JSON number.")
        return value

    @model_validator(mode="after")
    def usable_seed(self) -> ConfigPatch:
        if self.path == ("random_seed",) and (type(self.value) is not int or self.value < 0):
            raise ValueError("random_seed must be a non-negative integer.")
        return self


class PatchProposal(BaseModel):
    """Immutable review content; duplicate paths are ambiguous and rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    patches: tuple[ConfigPatch, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_paths(self) -> PatchProposal:
        paths = [patch.path for patch in self.patches]
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate ConfigPatch paths are not permitted.")
        return self

    @property
    def proposal_hash(self) -> str:
        return _hash(self.model_dump(mode="json"))


class PatchApproval(BaseModel):
    """Caller-attested human review bound to the exact approved patch content."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    approver_id: str = Field(min_length=1, max_length=128, pattern=r"^[\w.@+\-]+$")
    approved_at_utc: datetime
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    patches: tuple[ConfigPatch, ...]

    @field_validator("approved_at_utc")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Approval timestamp must include a timezone.")
        if value > datetime.now(timezone.utc):
            raise ValueError("Approval timestamp cannot be in the future.")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def matching_content(self) -> PatchApproval:
        if PatchProposal(patches=self.patches).proposal_hash != self.proposal_hash:
            raise ValueError("Approval proposal hash does not match approved patch content.")
        return self


def _audit(path: Path | str | None, event: str, **details: Any) -> None:
    if path is None:
        return
    audit_path = Path(path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "event": event, **details}
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, allow_nan=False) + "\n")


@dataclass(frozen=True)
class NLScenarioResult:
    proposal: PatchProposal | None
    diff_yaml: str
    used_llm: bool
    model_used: str
    prompt_hash: str
    timestamp_utc: str
    error: str | None = None
    proposal_hash: str | None = None
    audit_log_path: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.proposal is not None and self.proposal_hash is None:
            object.__setattr__(self, "proposal_hash", self.proposal.proposal_hash)

    def approve(self, *, approver_id: str) -> PatchApproval:
        """Call only after a human reviews this proposal. Never call automatically."""
        if self.proposal is None or self.proposal.proposal_hash != self.proposal_hash:
            raise GovernanceError("No unchanged proposal available for human approval.")
        return PatchApproval(
            approver_id=approver_id,
            approved_at_utc=datetime.now(timezone.utc),
            proposal_hash=self.proposal_hash,
            patches=self.proposal.patches,
        )

    def apply_to(
        self,
        config: EngineConfig,
        *,
        approved: bool = False,
        approval: PatchApproval | None = None,
    ) -> EngineConfig:
        """Apply only the reviewed content with explicit human identity and approval."""
        return apply_patches(
            config,
            self.proposal.patches if self.proposal else [],
            approved=approved,
            approval=approval,
            proposal_hash=self.proposal_hash,
            audit_log_path=self.audit_log_path,
            model_used=self.model_used,
            prompt_hash=self.prompt_hash,
        )


def _patched_config(config: EngineConfig, proposal: PatchProposal) -> EngineConfig:
    # Dump once, patch only this detached in-memory document, validate the complete batch once.
    data = config.model_dump()
    for patch in proposal.patches:
        target = data
        for key in patch.path[:-1]:
            target = target[key]
            if not isinstance(target, dict):
                raise ValueError("Patch requires an existing scenario section.")
        target[patch.path[-1]] = patch.value
    return EngineConfig.model_validate(data)


def apply_patches(
    config: EngineConfig,
    patches: list[ConfigPatch] | tuple[ConfigPatch, ...],
    *,
    approved: bool = False,
    approval: PatchApproval | None = None,
    proposal_hash: str | None = None,
    audit_log_path: Path | str | None = None,
    model_used: str = "manual",
    prompt_hash: str | None = None,
) -> EngineConfig:
    """Atomically validate and apply reviewed assumptions. A boolean alone is insufficient."""
    context: dict[str, Any] = {
        "model_used": model_used,
        "prompt_hash": prompt_hash,
        "proposal_hash": proposal_hash,
    }
    try:
        if approved is not True or approval is None:
            raise GovernanceError("Explicit human approval and approver identity are required.")
        # Reparse even frozen models: model_copy/model_construct are trusted Pydantic escape hatches.
        approval = PatchApproval.model_validate(approval.model_dump(mode="json"))
        context.update(
            proposal_hash=approval.proposal_hash,
            approver_id=approval.approver_id,
            approved_at_utc=approval.approved_at_utc.isoformat(),
            patches=PatchProposal(patches=approval.patches).model_dump(mode="json")["patches"],
        )
        proposal = PatchProposal.model_validate(
            {
                "patches": [patch.model_dump(mode="json") for patch in patches],
            }
        )
        if proposal.proposal_hash != approval.proposal_hash or (
            proposal_hash is not None and proposal_hash != approval.proposal_hash
        ):
            raise GovernanceError("Reviewed proposal does not match approved patch content.")
        _audit(audit_log_path, "approval", validation_outcome="approved", **context)
        result = _patched_config(config, proposal)
        context["config_hash"] = _hash(result.model_dump(mode="json"))
    except Exception as exc:
        # Never log raw exceptions: validation/SDK errors can echo a secret-bearing input.
        _audit(
            audit_log_path,
            "application_failed",
            validation_outcome="rejected",
            error_category=type(exc).__name__,
            **context,
        )
        raise
    _audit(
        audit_log_path,
        "application_succeeded",
        validation_outcome="valid",
        error_category=None,
        **context,
    )
    return result


class NLScenarioInterface:
    """Propose numeric scenario assumptions; a named human must approve before application.

    Usage::

        result = interface.propose(config, "cut the cash rate by 25 bps")
        print(result.diff_yaml)  # review the proposed change
        # Only after human review, using that human's authenticated caller identity:
        approval = result.approve(approver_id=human_identifier)
        new_config = interface.apply(config, result, approve=True, approval=approval)
    """

    def __init__(
        self,
        use_llm: bool = True,
        client: Any | None = None,
        model: str = _DEFAULT_MODEL,
        audit_log_path: Path | str | None = None,
        max_tokens: int = 400,
    ):
        self._use_llm = use_llm
        self._client = client
        self._model = model
        self._audit_log_path = Path(audit_log_path) if audit_log_path else None
        self._max_tokens = max_tokens

    def propose(self, config: EngineConfig, instruction: str) -> NLScenarioResult:
        """Generate and validate a proposal without applying it or logging raw prompts."""
        import yaml

        config_yaml = yaml.dump(config.model_dump(), default_flow_style=False)
        user_prompt = (
            f"Current config:\n```yaml\n{config_yaml}```\n\n"
            f"Instruction: {instruction}\n\n"
            "Return a JSON patches object."
        )
        prompt_hash = hashlib.sha256(user_prompt.encode()).hexdigest()
        proposal, used_llm, model_used, error = self._generate(user_prompt)
        context = {
            "used_llm": used_llm,
            "model_used": model_used,
            "prompt_hash": prompt_hash,
            "proposal_hash": proposal.proposal_hash if proposal else None,
        }
        _audit(
            self._audit_log_path,
            "proposal_generated",
            n_patches=(len(proposal.patches) if proposal else 0),
            validation_outcome="pending",
            patches=proposal.model_dump(mode="json")["patches"] if proposal else [],
            **context,
        )
        if proposal is not None:
            try:
                _patched_config(config, proposal)
            except Exception as exc:
                error = type(exc).__name__
                proposal = None
        if error is not None:
            _audit(
                self._audit_log_path,
                "proposal_rejected",
                validation_outcome="rejected",
                error_category=error,
                **context,
            )
        return NLScenarioResult(
            proposal=proposal,
            diff_yaml=_format_diff(proposal)
            if proposal
            else f"# LLM error: {error}\n# {_DECISION_SUPPORT_TAG}",
            used_llm=used_llm,
            model_used=model_used,
            prompt_hash=prompt_hash,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            error=error,
            audit_log_path=self._audit_log_path,
        )

    def apply(
        self,
        config: EngineConfig,
        proposal_result: NLScenarioResult,
        *,
        approve: bool,
        approval: PatchApproval | None = None,
    ) -> EngineConfig:
        """Return a validated config only after explicit, content-bound human approval."""
        return replace(proposal_result, audit_log_path=self._audit_log_path).apply_to(
            config,
            approved=approve,
            approval=approval,
        )

    def _generate(self, user_prompt: str) -> tuple[PatchProposal | None, bool, str, str | None]:
        if not self._use_llm:
            return None, False, "disabled", "LLM disabled"
        client = self._client
        if client is None:
            try:
                from balance_pricing_engine.llm._client import build_client

                client = build_client()
            except ImportError:
                return None, False, "fallback", "anthropic package not installed"
            except Exception as exc:
                return None, False, "fallback", type(exc).__name__
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = response.content[0].text.strip()
            parsed = json.loads(raw)
            proposal = PatchProposal(**parsed)
            return proposal, True, self._model, None
        except Exception as exc:
            category = type(exc).__name__
            logging.getLogger(__name__).warning(
                "NLScenarioInterface proposal rejected: %s", category
            )
            return None, False, self._model, category


def _format_diff(proposal: PatchProposal) -> str:
    lines = ["# Proposed config changes (review before approving)", ""]
    for patch in proposal.patches:
        field_path = ".".join(patch.path)
        lines.append(f"  {field_path}: {patch.value!r}")
    lines.append("")
    lines.append(f"# {_DECISION_SUPPORT_TAG}")
    return "\n".join(lines)
