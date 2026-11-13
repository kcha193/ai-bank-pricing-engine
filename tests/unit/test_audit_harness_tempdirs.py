"""Audit scratch directories must initialise without checkout-local state."""

import ast
import tempfile
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "script_name",
    ["phase8_governance.py", "phase2_numeric.py", "phase2_extra.py"],
)
def test_audit_scratch_directory_on_fresh_checkout(script_name, tmp_path, monkeypatch):
    source = Path("docs/audits/2026-11-13-evidence/mlgov") / script_name
    evidence = tmp_path / "checkout" / source.parent
    evidence.mkdir(parents=True)
    script = evidence / script_name
    script.write_text(source.read_text())
    committed_result = evidence / "results.json"
    committed_result.write_text("original evidence")
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))

    # Execute only the actual allocation expression: model training is expensive
    # and unrelated to whether scratch storage can initialise on a fresh checkout.
    allocations = [
        node
        for node in ast.walk(ast.parse(script.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "tempfile"
        and node.func.attr == "mkdtemp"
    ]
    assert len(allocations) == 1
    code = compile(ast.Expression(allocations[0]), str(script), "eval")
    namespace = {
        "Path": Path,
        "tempfile": tempfile,
        "__file__": str(script),
        "HERE": evidence,
    }
    first = Path(eval(code, namespace))
    second = Path(eval(code, namespace))

    assert first.is_dir() and second.is_dir()
    assert first != second
    assert first.parent == second.parent == system_temp
    (first / committed_result.name).write_text("scratch data")
    assert committed_result.read_text() == "original evidence"
    assert not (evidence / "tmp").exists()
