# P2-10: Run-Ledger Checklist Sign-off Functions

> **STATUS: ✅ COMPLETE** — run-ledger checklist sign-off functions — PR #36

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `sign_off_item(run_dir, item_id, approver, *, notes)` and `sign_off_all_items(run_dir, approver, *, notes)` to `run_ledger/checklist.py`. These update the existing `governance_checklist.json` in-place and regenerate `governance_checklist.md` to reflect current sign-off state, enabling a governed review workflow via Python rather than manual file editing.

**Architecture:** The JSON schema already has the necessary fields (`status`, `signed_by`, `signed_at`, `notes`) — they just start as `"pending"` / `null`. The two new functions read the JSON, mutate the matching item(s), write it back, and rebuild the markdown from the updated JSON. No new dependencies. The `_build_checklist` function is internal — we add a companion `_build_checklist_from_json` that renders from the JSON payload so both formats stay in sync.

**Tech Stack:** Python 3.9+, stdlib only (`json`, `datetime`, `pathlib`), pytest.

---

## File map

| Action | Path | Responsibility |
| --- | --- | --- |
| Modify | `src/balance_pricing_engine/run_ledger/checklist.py` | Add `sign_off_item`, `sign_off_all_items`, `_rebuild_markdown_from_json` |
| Modify | `tests/unit/test_run_ledger.py` | Tests for the two new public functions |

---

## Background: JSON item schema

Each item in `governance_checklist.json` looks like:

```json
{
  "id": "boundary-result-reviewed",
  "section": "Optimizer Diagnostics",
  "label": "Boundary result reviewed",
  "status": "pending",
  "notes": null,
  "signed_by": null,
  "signed_at": null
}
```

Valid `status` values: `"pending"`, `"approved"`, `"flagged"`.

Known item IDs (from `_build_checklist_json`):

- `boundary-result-reviewed`
- `sensitivity-reviewed`
- `competitor-rate-freshness`
- `funding-assumptions-reviewed`
- `elasticity-assumptions-reviewed`
- `conduct-fairness-noted`
- `human-approval`
- `synthetic-data-acknowledged`

---

## Task 1: `sign_off_item` and `sign_off_all_items`

**Files:**

- Modify: `src/balance_pricing_engine/run_ledger/checklist.py`
- Test: `tests/unit/test_run_ledger.py`

- [ ] **Step 1: Understand the existing run-ledger test patterns**

```bash
grep -n "def test_\|governance_checklist\|write_governance" tests/unit/test_run_ledger.py | head -20
```

This shows how the existing tests create a run directory and call `write_governance_checklist`.

- [ ] **Step 2: Write the failing tests**

Add to `tests/unit/test_run_ledger.py`:

```python
import json
from balance_pricing_engine.run_ledger.checklist import sign_off_item, sign_off_all_items


def _checklist_json(run_dir) -> dict:
    return json.loads((run_dir / "governance_checklist.json").read_text())


def test_sign_off_item_updates_status_and_signer(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    sign_off_item(run_dir, "human-approval", approver="alice")

    payload = _checklist_json(run_dir)
    item = next(i for i in payload["items"] if i["id"] == "human-approval")
    assert item["status"] == "approved"
    assert item["signed_by"] == "alice"
    assert item["signed_at"] is not None


def test_sign_off_item_records_notes(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    sign_off_item(run_dir, "boundary-result-reviewed", approver="bob", notes="Reviewed, clear.")

    payload = _checklist_json(run_dir)
    item = next(i for i in payload["items"] if i["id"] == "boundary-result-reviewed")
    assert item["notes"] == "Reviewed, clear."


def test_sign_off_item_raises_on_unknown_id(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    with pytest.raises(ValueError, match="not found"):
        sign_off_item(run_dir, "nonexistent-item-id", approver="alice")


def test_sign_off_item_raises_if_json_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        sign_off_item(tmp_path, "human-approval", approver="alice")


def test_sign_off_item_regenerates_markdown(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    sign_off_item(run_dir, "human-approval", approver="alice")

    md = (run_dir / "governance_checklist.md").read_text()
    assert "alice" in md
    assert "approved" in md.lower()


def test_sign_off_all_items_marks_all_approved(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    sign_off_all_items(run_dir, approver="carol")

    payload = _checklist_json(run_dir)
    for item in payload["items"]:
        assert item["status"] == "approved"
        assert item["signed_by"] == "carol"


def test_sign_off_all_items_applies_shared_notes(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    sign_off_all_items(run_dir, approver="carol", notes="Batch approved after review.")

    payload = _checklist_json(run_dir)
    for item in payload["items"]:
        assert item["notes"] == "Batch approved after review."
```

Also add the `run_dir_with_checklist` fixture. Find the existing `conftest.py` or test file that creates a run directory — if it's inline, add the fixture to `test_run_ledger.py`:

```python
import pytest
from balance_pricing_engine.run_ledger.checklist import write_governance_checklist
from balance_pricing_engine.run_ledger.metadata import RunMetadata


@pytest.fixture()
def run_dir_with_checklist(tmp_path):
    """A tmp run directory with governance_checklist.json + .md already written."""
    metadata = RunMetadata(
        run_id="test-run-001",
        scenario_name="test_scenario",
        timestamp="2026-05-23T00:00:00+00:00",
        git_commit=None,
        config_path="configs/base.yaml",
        config_hash="abc123",
        python_version="3.11.0",
        package_version="0.1.0",
        boundary_warning_flag=False,
        initial_boundary_warning_flag=False,
        expansion_rounds=0,
        policy_band_breached=False,
        competitor_rate_source=None,
    )
    # write_governance_checklist needs a config and diagnostics — use the simplest possible stubs
    # by calling _build_checklist_json directly instead.
    from balance_pricing_engine.run_ledger.checklist import _build_checklist_json
    import json
    json_content = _build_checklist_json(metadata)
    (tmp_path / "governance_checklist.json").write_text(json_content, encoding="utf-8")
    (tmp_path / "governance_checklist.md").write_text("# placeholder\n", encoding="utf-8")
    return tmp_path
```

- [ ] **Step 3: Run the tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_run_ledger.py::test_sign_off_item_updates_status_and_signer -v
```

Expected: `ImportError` — `sign_off_item` not defined.

- [ ] **Step 4: Implement `sign_off_item`, `sign_off_all_items`, `_rebuild_markdown_from_json` in `checklist.py`**

Add the following at the end of `src/balance_pricing_engine/run_ledger/checklist.py`:

```python
def sign_off_item(
    run_dir: Path,
    item_id: str,
    approver: str,
    *,
    notes: str | None = None,
    status: str = "approved",
) -> None:
    """Mark one checklist item as signed off; update JSON and regenerate markdown."""
    json_path = Path(run_dir) / "governance_checklist.json"
    if not json_path.exists():
        raise FileNotFoundError(f"governance_checklist.json not found in {run_dir}")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    matched = [item for item in payload["items"] if item["id"] == item_id]
    if not matched:
        raise ValueError(
            f"Checklist item '{item_id}' not found. "
            f"Known IDs: {[i['id'] for i in payload['items']]}"
        )
    from datetime import datetime, timezone
    signed_at = datetime.now(timezone.utc).isoformat()
    for item in matched:
        item["status"] = status
        item["signed_by"] = approver
        item["signed_at"] = signed_at
        if notes is not None:
            item["notes"] = notes
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    _rebuild_markdown_from_json(Path(run_dir), payload)


def sign_off_all_items(
    run_dir: Path,
    approver: str,
    *,
    notes: str | None = None,
    status: str = "approved",
) -> None:
    """Mark every checklist item as signed off; update JSON and regenerate markdown."""
    json_path = Path(run_dir) / "governance_checklist.json"
    if not json_path.exists():
        raise FileNotFoundError(f"governance_checklist.json not found in {run_dir}")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    from datetime import datetime, timezone
    signed_at = datetime.now(timezone.utc).isoformat()
    for item in payload["items"]:
        item["status"] = status
        item["signed_by"] = approver
        item["signed_at"] = signed_at
        if notes is not None:
            item["notes"] = notes
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    _rebuild_markdown_from_json(Path(run_dir), payload)


def _rebuild_markdown_from_json(run_dir: Path, payload: dict) -> None:
    """Regenerate governance_checklist.md from an already-loaded JSON payload."""
    lines = [
        f"# Governance Checklist — {payload['scenario_name']}",
        "",
        f"Run: `{payload['run_id']}`  ",
        f"Timestamp: {payload['timestamp']}  ",
        f"Config hash: `{payload['config_hash']}`  ",
        "",
        "All items must be reviewed and signed off before this run's recommendation is accepted.",
        "",
        "---",
        "",
    ]
    current_section: str | None = None
    for item in payload["items"]:
        section = item.get("section", "")
        if section != current_section:
            if current_section is not None:
                lines.extend(["", "---", ""])
            lines.extend([f"## {section}", ""])
            current_section = section

        status = item["status"]
        check = "x" if status == "approved" else " "
        label = item["label"]
        signed_by = item.get("signed_by") or ""
        signed_at = item.get("signed_at") or ""
        notes_text = item.get("notes") or ""

        line = f"- [{check}] **{label}** — status: {status}"
        if signed_by:
            line += f" | signed by: {signed_by} at {signed_at}"
        if notes_text:
            line += f"\n  - Notes: {notes_text}"
        lines.append(line)
        lines.append("")

    lines.append(f"_Generated by {payload.get('generated_by', 'ai-bank-pricing-engine')}_")
    (run_dir / "governance_checklist.md").write_text("\n".join(lines), encoding="utf-8")
```

- [ ] **Step 5: Run the new tests**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v -k "sign_off"
```

Expected: all 7 new tests PASS.

- [ ] **Step 6: Run the full test suite**

```bash
python3 -m pytest -q
```

Expected: same or higher pass count.

- [ ] **Step 7: Commit**

```bash
git add src/balance_pricing_engine/run_ledger/checklist.py tests/unit/test_run_ledger.py
git commit -m "feat: add sign_off_item and sign_off_all_items to run-ledger checklist"
```

---

## Self-review

**Spec coverage:**

- `sign_off_item` updates `status`, `signed_by`, `signed_at`, `notes` in the JSON ✓
- `sign_off_item` raises `FileNotFoundError` when JSON missing ✓
- `sign_off_item` raises `ValueError` when item ID unknown ✓
- `sign_off_all_items` signs every item ✓
- Markdown is regenerated after every sign-off ✓ (`_rebuild_markdown_from_json`)
- Regenerated markdown reflects current sign-off state (checked box + signer) ✓

**Placeholder scan:** None found.

**Type consistency:** Both public functions accept `Path` (widened to `Path | str` implicitly via `Path(run_dir)`). `status` defaults to `"approved"`. `_rebuild_markdown_from_json` takes `dict` — same shape as `json.loads` output from the file written by `_build_checklist_json`.
