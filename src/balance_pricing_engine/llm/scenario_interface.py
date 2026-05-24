"""Natural language scenario-run interface. Decision-support only; requires human approval."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from balance_pricing_engine.config import EngineConfig

_DECISION_SUPPORT_TAG = "[DECISION-SUPPORT: requires human review]"
_DEFAULT_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = (
    "You are a pricing configuration assistant. "
    "Given a natural language instruction and the current EngineConfig YAML, "
    "return ONLY a JSON object with a 'patches' list. "
    "Each patch must have 'path' (list of string keys) and 'value' (the new value). "
    "Only output the JSON - no explanation, no markdown. "
    "Example: {\"patches\": [{\"path\": [\"market\", \"cash_rate\"], \"value\": 0.0475}]}"
)


class ConfigPatch(BaseModel):
    """A single config field change: dot-path + new value."""

    path: list[str]
    value: Any


class PatchProposal(BaseModel):
    """Validated output from the LLM: a list of config patches to propose."""

    patches: list[ConfigPatch]


@dataclass(frozen=True)
class NLScenarioResult:
    proposal: PatchProposal | None
    diff_yaml: str
    used_llm: bool
    model_used: str
    prompt_hash: str
    timestamp_utc: str
    error: str | None = None


def apply_patches(config: EngineConfig, patches: list[ConfigPatch]) -> EngineConfig:
    """Apply a list of ConfigPatch objects to an EngineConfig, returning a new config.

    Uses model_copy at each nesting level. Raises AttributeError for unknown paths,
    ValidationError for type mismatches.
    """
    result = config
    for patch in patches:
        result = _apply_single_patch(result, patch.path, patch.value)
    return result


def _apply_single_patch(config: EngineConfig, path: list[str], value: Any) -> EngineConfig:
    if not path:
        raise ValueError("ConfigPatch path must not be empty.")
    if len(path) == 1:
        field = path[0]
        if not hasattr(config, field):
            raise AttributeError(f"EngineConfig has no field '{field}'.")
        return _validated_copy(config, {field: value})
    field = path[0]
    if not hasattr(config, field):
        raise AttributeError(f"EngineConfig has no field '{field}'.")
    sub_model = getattr(config, field)
    if not hasattr(sub_model, "model_copy"):
        raise AttributeError(
            f"Field '{field}' is not a Pydantic model; cannot patch nested path {path}."
        )
    updated_sub = _apply_nested(sub_model, path[1:], value)
    return _validated_copy(config, {field: updated_sub})


def _apply_nested(model: Any, path: list[str], value: Any) -> Any:
    if not path:
        raise ValueError("ConfigPatch path must not be empty.")
    if len(path) == 1:
        field = path[0]
        if not hasattr(model, field):
            raise AttributeError(f"Model {type(model).__name__} has no field '{field}'.")
        return _validated_copy(model, {field: value})
    field = path[0]
    if not hasattr(model, field):
        raise AttributeError(f"Model {type(model).__name__} has no field '{field}'.")
    sub = getattr(model, field)
    if not hasattr(sub, "model_copy"):
        raise AttributeError(
            f"Field '{field}' is not a Pydantic model; cannot patch nested path {path}."
        )
    return _validated_copy(model, {field: _apply_nested(sub, path[1:], value)})


def _validated_copy(model: Any, update: dict[str, Any]) -> Any:
    data = model.model_dump()
    data.update(update)
    return type(model).model_validate(data)


class NLScenarioInterface:
    """Parse a natural-language instruction into a config diff and apply it on approval.

    Usage::

        interface = NLScenarioInterface()
        result = interface.propose(config, "cut the cash rate by 25 bps")
        print(result.diff_yaml)  # review the proposed change
        if approved:
            new_config = interface.apply(config, result, approve=True)
            diagnostics = run_baseline_diagnostics(new_config)
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
        """Call the LLM with the instruction; return a proposal for human review."""
        import yaml

        config_yaml = yaml.dump(config.model_dump(), default_flow_style=False)
        user_prompt = (
            f"Current config:\n```yaml\n{config_yaml}```\n\n"
            f"Instruction: {instruction}\n\n"
            "Return a JSON patches object."
        )
        prompt_hash = hashlib.sha256(user_prompt.encode()).hexdigest()[:16]
        timestamp = datetime.now(timezone.utc).isoformat()

        proposal, used_llm, model_used, error = self._generate(user_prompt)
        diff_yaml = _format_diff(proposal) if proposal else f"# LLM error: {error}"

        result = NLScenarioResult(
            proposal=proposal,
            diff_yaml=diff_yaml,
            used_llm=used_llm,
            model_used=model_used,
            prompt_hash=prompt_hash,
            timestamp_utc=timestamp,
            error=error,
        )
        self._write_audit_log(result, user_prompt)
        return result

    def apply(
        self,
        config: EngineConfig,
        proposal_result: NLScenarioResult,
        *,
        approve: bool,
    ) -> EngineConfig:
        """Apply the proposed patches if approve=True; raise otherwise."""
        if not approve:
            raise ValueError(
                "Human approval required. Call apply(..., approve=True) to confirm. "
                f"{_DECISION_SUPPORT_TAG}"
            )
        if proposal_result.proposal is None:
            raise ValueError(
                "No valid proposal to apply. "
                f"Error: {proposal_result.error}"
            )
        return apply_patches(config, proposal_result.proposal.patches)

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
                return None, False, "fallback", str(exc)
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
            logging.getLogger(__name__).warning("NLScenarioInterface LLM call failed: %s", exc)
            return None, False, "fallback", str(exc)

    def _write_audit_log(self, result: NLScenarioResult, user_prompt: str) -> None:
        if self._audit_log_path is None:
            return
        self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp_utc": result.timestamp_utc,
            "used_llm": result.used_llm,
            "model_used": result.model_used,
            "prompt_hash": result.prompt_hash,
            "n_patches": len(result.proposal.patches) if result.proposal else 0,
            "error": result.error,
        }
        with open(self._audit_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def _format_diff(proposal: PatchProposal) -> str:
    lines = ["# Proposed config changes (review before approving)", ""]
    for patch in proposal.patches:
        field_path = ".".join(patch.path)
        lines.append(f"  {field_path}: {patch.value!r}")
    lines.append("")
    lines.append(f"# {_DECISION_SUPPORT_TAG}")
    return "\n".join(lines)
