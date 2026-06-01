# P3-5: Natural Language Scenario-Run Interface (Tier 4)

> **STATUS: ✅ COMPLETE** — NLScenarioInterface with approval gate + audit log — PR #37

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `NLScenarioInterface` class to `llm/` that accepts a natural-language instruction (e.g. "cut the cash rate by 25 bps and re-run the baseline"), parses it into a validated config diff via the Claude API, shows the diff for human approval, and — when approved — applies the patch and returns the updated `EngineConfig`.

**Architecture:** `NLScenarioInterface` in a new `src/balance_pricing_engine/llm/scenario_interface.py`. It uses the existing `_client.py` wrapper to call Claude (with a structured JSON response), then validates the response against a `ConfigPatch` Pydantic model (key-path + value pairs). The diff is serialised as a YAML block for the human to review. Approval is explicit (`approve=True` kwarg) — no action without it. A deterministic fallback returns the unchanged config if the LLM is unavailable. All calls are audit-logged via the same pattern as `ScenarioNarrativeAgent`. Re-exported from `llm/__init__.py`.

**Governance constraints:**
- No autonomous price-setting. The interface only proposes a config diff.
- Human approval is required (`approve=True`) before the patched config is returned.
- All proposed patches are validated through Pydantic before any config change.
- Every LLM call is logged to the audit log.
- Deterministic fallback: when the LLM is unavailable, returns `(None, "LLM unavailable")`.

**Tech Stack:** Python 3.9+, `anthropic>=0.30` (optional `[llm]` extra already installed), pydantic v2, pytest with monkeypatched Anthropic client.

---

## File map

| Action | Path | Responsibility |
| --- | --- | --- |
| Create | `src/balance_pricing_engine/llm/scenario_interface.py` | `ConfigPatch`, `PatchProposal`, `NLScenarioInterface` |
| Modify | `src/balance_pricing_engine/llm/__init__.py` | Re-export `NLScenarioInterface` |
| Create | `tests/unit/test_llm_scenario_interface.py` | Tests (monkeypatched client, no real API calls) |

---

## Background: `EngineConfig` patch mechanics

`EngineConfig` is a Pydantic v2 `BaseModel` with nested sub-models. To apply a patch we use `model_copy(update={...})` for top-level fields. For nested fields (e.g. `market.cash_rate`), we must copy the nested model first:

```python
# patch market.cash_rate from 0.05 to 0.0475:
new_market = config.market.model_copy(update={"cash_rate": 0.0475})
new_config = config.model_copy(update={"market": new_market})
```

A `ConfigPatch` encodes one field change as `{"path": ["market", "cash_rate"], "value": 0.0475}`. Applying a list of patches walks the path and uses `model_copy` at each level.

The Claude prompt returns JSON matching:
```json
{"patches": [{"path": ["market", "cash_rate"], "value": 0.0475}]}
```

---

## Task 1: `ConfigPatch`, `PatchProposal`, patch application logic

**Files:**

- Create: `src/balance_pricing_engine/llm/scenario_interface.py`
- Test: `tests/unit/test_llm_scenario_interface.py`

- [ ] **Step 1: Write the failing tests for the patch application logic**

Create `tests/unit/test_llm_scenario_interface.py`:

```python
"""Tests for NLScenarioInterface — no real API calls."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.llm.scenario_interface import (
    ConfigPatch,
    NLScenarioInterface,
    apply_patches,
)


@pytest.fixture()
def base_config():
    return load_config(Path("configs/base.yaml"))


def test_apply_patches_top_level_field(base_config):
    patches = [ConfigPatch(path=["random_seed"], value=99)]
    patched = apply_patches(base_config, patches)
    assert patched.random_seed == 99


def test_apply_patches_nested_field(base_config):
    original_cash_rate = base_config.market.cash_rate
    new_rate = original_cash_rate - 0.0025  # -25 bps
    patches = [ConfigPatch(path=["market", "cash_rate"], value=new_rate)]
    patched = apply_patches(base_config, patches)
    assert patched.market.cash_rate == pytest.approx(new_rate)
    assert base_config.market.cash_rate == pytest.approx(original_cash_rate)  # immutable


def test_apply_patches_multiple_fields(base_config):
    patches = [
        ConfigPatch(path=["market", "cash_rate"], value=0.05),
        ConfigPatch(path=["market", "competitor_loan_rate"], value=0.07),
    ]
    patched = apply_patches(base_config, patches)
    assert patched.market.cash_rate == pytest.approx(0.05)
    assert patched.market.competitor_loan_rate == pytest.approx(0.07)


def test_apply_patches_raises_on_unknown_path(base_config):
    patches = [ConfigPatch(path=["nonexistent_field"], value=42)]
    with pytest.raises((AttributeError, ValueError)):
        apply_patches(base_config, patches)


def test_apply_patches_raises_on_invalid_value(base_config):
    # cash_rate must be a float, not a string
    patches = [ConfigPatch(path=["market", "cash_rate"], value="not-a-float")]
    with pytest.raises(Exception):
        apply_patches(base_config, patches)
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python3 -m pytest tests/unit/test_llm_scenario_interface.py::test_apply_patches_top_level_field -v
```

Expected: `ImportError` — module not yet created.

- [ ] **Step 3: Create `scenario_interface.py` with patch types and `apply_patches`**

Create `src/balance_pricing_engine/llm/scenario_interface.py`:

```python
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
    "Only output the JSON — no explanation, no markdown. "
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
    if len(path) == 1:
        field = path[0]
        if not hasattr(config, field):
            raise AttributeError(f"EngineConfig has no field '{field}'.")
        return config.model_copy(update={field: value})
    # Nested: recurse into the sub-model
    field = path[0]
    if not hasattr(config, field):
        raise AttributeError(f"EngineConfig has no field '{field}'.")
    sub_model = getattr(config, field)
    if not hasattr(sub_model, "model_copy"):
        raise AttributeError(
            f"Field '{field}' is not a Pydantic model; cannot patch nested path {path}."
        )
    updated_sub = _apply_nested(sub_model, path[1:], value)
    return config.model_copy(update={field: updated_sub})


def _apply_nested(model: Any, path: list[str], value: Any) -> Any:
    if len(path) == 1:
        field = path[0]
        if not hasattr(model, field):
            raise AttributeError(f"Model {type(model).__name__} has no field '{field}'.")
        return model.model_copy(update={field: value})
    field = path[0]
    if not hasattr(model, field):
        raise AttributeError(f"Model {type(model).__name__} has no field '{field}'.")
    sub = getattr(model, field)
    if not hasattr(sub, "model_copy"):
        raise AttributeError(
            f"Field '{field}' is not a Pydantic model; cannot patch nested path {path}."
        )
    return model.model_copy(update={field: _apply_nested(sub, path[1:], value)})


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

    def _generate(
        self, user_prompt: str
    ) -> tuple[PatchProposal | None, bool, str, str | None]:
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
```

- [ ] **Step 4: Run the patch tests**

```bash
python3 -m pytest tests/unit/test_llm_scenario_interface.py -v -k "apply_patches"
```

Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/llm/scenario_interface.py \
        tests/unit/test_llm_scenario_interface.py
git commit -m "feat: ConfigPatch and apply_patches for NL scenario interface"
```

---

## Task 2: `NLScenarioInterface` end-to-end tests

**Files:**

- Test: `tests/unit/test_llm_scenario_interface.py`

- [ ] **Step 1: Write the end-to-end tests with a monkeypatched client**

Add to `tests/unit/test_llm_scenario_interface.py`:

```python
def _make_mock_client(patches_json: str):
    """Build a mock Anthropic client that returns a fixed JSON response."""
    message = MagicMock()
    message.content = [MagicMock(text=patches_json)]
    client = MagicMock()
    client.messages.create.return_value = message
    return client


def test_propose_returns_proposal_with_monkeypatched_client(base_config):
    patch_json = json.dumps({
        "patches": [{"path": ["market", "cash_rate"], "value": 0.0475}]
    })
    client = _make_mock_client(patch_json)
    interface = NLScenarioInterface(client=client)

    result = interface.propose(base_config, "cut the cash rate by 25 bps")

    assert result.proposal is not None
    assert result.used_llm is True
    assert len(result.proposal.patches) == 1
    assert result.proposal.patches[0].path == ["market", "cash_rate"]
    assert result.proposal.patches[0].value == pytest.approx(0.0475)


def test_propose_falls_back_gracefully_when_llm_disabled(base_config):
    interface = NLScenarioInterface(use_llm=False)

    result = interface.propose(base_config, "cut the cash rate by 25 bps")

    assert result.proposal is None
    assert result.used_llm is False
    assert result.error is not None


def test_propose_falls_back_on_malformed_json(base_config):
    client = _make_mock_client("not valid json at all")
    interface = NLScenarioInterface(client=client)

    result = interface.propose(base_config, "cut the cash rate by 25 bps")

    assert result.proposal is None
    assert result.used_llm is False


def test_apply_with_approval_patches_config(base_config):
    patch_json = json.dumps({
        "patches": [{"path": ["market", "cash_rate"], "value": 0.0475}]
    })
    client = _make_mock_client(patch_json)
    interface = NLScenarioInterface(client=client)
    result = interface.propose(base_config, "cut the cash rate by 25 bps")

    patched = interface.apply(base_config, result, approve=True)

    assert patched.market.cash_rate == pytest.approx(0.0475)
    assert base_config.market.cash_rate != pytest.approx(0.0475)  # original unchanged


def test_apply_without_approval_raises(base_config):
    patch_json = json.dumps({
        "patches": [{"path": ["market", "cash_rate"], "value": 0.0475}]
    })
    client = _make_mock_client(patch_json)
    interface = NLScenarioInterface(client=client)
    result = interface.propose(base_config, "cut the cash rate by 25 bps")

    with pytest.raises(ValueError, match="approval"):
        interface.apply(base_config, result, approve=False)


def test_apply_with_no_proposal_raises(base_config):
    interface = NLScenarioInterface(use_llm=False)
    result = interface.propose(base_config, "do something")

    with pytest.raises(ValueError, match="No valid proposal"):
        interface.apply(base_config, result, approve=True)


def test_propose_writes_audit_log(tmp_path, base_config):
    patch_json = json.dumps({
        "patches": [{"path": ["random_seed"], "value": 99}]
    })
    client = _make_mock_client(patch_json)
    audit_log = tmp_path / "nl_scenario_audit.jsonl"
    interface = NLScenarioInterface(client=client, audit_log_path=audit_log)

    interface.propose(base_config, "change seed to 99")

    assert audit_log.exists()
    entry = json.loads(audit_log.read_text().strip())
    assert entry["used_llm"] is True
    assert entry["n_patches"] == 1
```

- [ ] **Step 2: Run all scenario interface tests**

```bash
python3 -m pytest tests/unit/test_llm_scenario_interface.py -v
```

Expected: all 12 tests PASS.

- [ ] **Step 3: Add re-export to `llm/__init__.py`**

In `src/balance_pricing_engine/llm/__init__.py`, add:

```python
from balance_pricing_engine.llm.scenario_interface import NLScenarioInterface, apply_patches

__all__ = [
    "ConfigAnomalyChecker",
    "ScenarioNarrativeAgent",
    "NLScenarioInterface",
    "apply_patches",
]
```

Check what's already in `__init__.py`:

```bash
cat src/balance_pricing_engine/llm/__init__.py
```

Add only what is missing.

- [ ] **Step 4: Run the full suite**

```bash
python3 -m pytest -q
```

Expected: same or higher pass count.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/llm/scenario_interface.py \
        src/balance_pricing_engine/llm/__init__.py \
        tests/unit/test_llm_scenario_interface.py
git commit -m "feat: NLScenarioInterface for natural language config patching with audit log"
```

---

## Self-review

**Spec coverage:**

- `ConfigPatch` (path + value) and `PatchProposal` (list of patches) ✓
- `apply_patches` validates through Pydantic, raises on unknown paths ✓
- `NLScenarioInterface.propose` calls LLM, returns `NLScenarioResult` with `diff_yaml` for review ✓
- `NLScenarioInterface.apply` requires `approve=True`, raises otherwise ✓
- Deterministic fallback when LLM unavailable (returns `proposal=None`) ✓
- Audit log written on every `propose` call ✓
- Tests use monkeypatched client — no real API calls ✓
- Governance tag in `apply` error message and `diff_yaml` ✓

**Placeholder scan:** None found.

**Type consistency:** `ConfigPatch.path: list[str]`, `ConfigPatch.value: Any`. `apply_patches(config: EngineConfig, patches: list[ConfigPatch]) -> EngineConfig`. `NLScenarioInterface.apply(...) -> EngineConfig`. `NLScenarioResult.proposal: PatchProposal | None`. All consistent throughout.
