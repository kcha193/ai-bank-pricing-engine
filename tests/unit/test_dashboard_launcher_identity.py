"""OS ownership and health probes are stubbed: no server or browser is started."""

import os
import subprocess
from unittest.mock import Mock

import pytest

from balance_pricing_engine.dashboard import launcher_identity as identity

COMMAND = [
    "/repo/.venv/bin/python",
    "-m",
    "streamlit",
    "run",
    "/repo/app.py",
    "--server.address",
    "127.0.0.1",
    "--server.port",
    "8765",
    "--server.headless",
    "true",
    "--browser.gatherUsageStats",
    "false",
]


@pytest.fixture
def probes(monkeypatch):
    values = {
        "listeners": "p42\n",
        "command": " ".join(COMMAND),
        "uid": str(os.getuid()),
        "ppid": "11",
        "comm": COMMAND[0],
        "lstart": "Sun Oct 11 00:00:00 2026",
    }

    def run(args, **kwargs):
        key = "listeners" if args[0] == "lsof" else args[-1].rstrip("=")
        return subprocess.CompletedProcess(args, 0, stdout=values[key], stderr="")

    monkeypatch.setattr(identity.subprocess, "run", run)
    health = Mock(return_value=True)
    monkeypatch.setattr(identity, "_healthy", health)
    return values, health


def test_matching_local_process_is_reused(probes):
    assert identity.verified_listener(8765, COMMAND) is not None


@pytest.mark.parametrize(
    "key,value",
    [
        ("command", "python -m streamlit run /other/app.py"),
        ("command", "python -m http.server 8765"),
        ("listeners", ""),
        ("listeners", "p42\np43\n"),
        ("lstart", ""),
        ("uid", "999999"),
    ],
)
def test_unknown_or_unrelated_identity_is_never_reused(probes, key, value):
    values, health = probes
    values[key] = value
    assert identity.verified_listener(8765, COMMAND) is None
    assert not health.called


def test_missing_os_identity_fails_closed(monkeypatch):
    monkeypatch.setattr(identity.subprocess, "run", Mock(side_effect=FileNotFoundError))
    assert identity.verified_listener(8765, COMMAND) is None


def test_slow_os_probe_fails_closed(monkeypatch):
    monkeypatch.setattr(
        identity.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("ps", 2))
    )
    assert identity.verified_listener(8765, COMMAND) is None


def test_unhealthy_correct_process_is_not_reused(probes):
    probes[1].return_value = False
    assert identity.verified_listener(8765, COMMAND) is None


def test_startup_only_opens_the_launchers_child(probes):
    assert identity.verified_listener(8765, COMMAND, parent_pid=11)
    assert identity.verified_listener(8765, COMMAND, parent_pid=12) is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("listeners", ""),
        ("listeners", "p43\n"),
        ("lstart", "Sun Oct 11 00:01:00 2026"),
        ("command", "python -m http.server 8765"),
    ],
)
def test_exit_or_port_replacement_during_health_probe_fails_closed(probes, key, value):
    values, health = probes

    def replace_process(port):
        values[key] = value
        return True

    health.side_effect = replace_process
    assert identity.verified_listener(8765, COMMAND) is None


def test_previously_verified_identity_must_still_match(probes):
    token = identity.verified_listener(8765, COMMAND)
    assert identity.verified_listener(8765, COMMAND, expected_identity=token)
    probes[0]["lstart"] = "Sun Oct 11 00:01:00 2026"
    assert identity.verified_listener(8765, COMMAND, expected_identity=token) is None


@pytest.mark.parametrize(
    "body,status,expected", [(b"ok", 200, True), (b"ok!", 200, False), (b"ok", 503, False)]
)
def test_health_requires_an_exact_successful_response_without_proxies(
    monkeypatch, body, status, expected
):
    from contextlib import nullcontext

    response = Mock(status=status)
    response.read.return_value = body
    opener = Mock()
    opener.open.return_value = nullcontext(response)
    factory = Mock(return_value=opener)
    monkeypatch.setattr(identity, "build_opener", factory)
    assert identity._healthy(8765) is expected
    assert factory.call_args.args[0].proxies == {}
    opener.open.assert_called_once_with("http://127.0.0.1:8765/_stcore/health", timeout=2)


@pytest.mark.parametrize("error", [TimeoutError(), OSError("unreachable")])
def test_unresponsive_health_fails_closed(monkeypatch, error):
    opener = Mock()
    opener.open.side_effect = error
    monkeypatch.setattr(identity, "build_opener", Mock(return_value=opener))
    assert not identity._healthy(8765)


def test_framework_python_identity_supports_virtualenv_launches(probes):
    values, _ = probes
    canonical = "/Python.framework/Resources/Python.app/Contents/MacOS/Python"
    values["comm"] = canonical
    values["command"] = " ".join([canonical, *COMMAND[1:]])
    assert identity.verified_listener(8765, COMMAND)


@pytest.mark.parametrize("verified", [True, False])
def test_cli_reports_only_a_verified_listener(monkeypatch, capsys, verified):
    monkeypatch.setattr(
        identity.sys, "argv", ["probe.py", "8765", "/repo/app.py", COMMAND[0], "11", "known"]
    )
    probe = Mock(return_value="known" if verified else None)
    monkeypatch.setattr(identity, "verified_listener", probe)
    assert identity.main() == (0 if verified else 1)
    assert capsys.readouterr().out == ("known\n" if verified else "")
    probe.assert_called_once_with(8765, COMMAND, parent_pid=11, expected_identity="known")


def test_cli_empty_optional_arguments_allow_reuse_probe(monkeypatch):
    monkeypatch.setattr(
        identity.sys, "argv", ["probe.py", "8765", "/repo/app.py", COMMAND[0], "", ""]
    )
    probe = Mock(return_value=None)
    monkeypatch.setattr(identity, "verified_listener", probe)
    assert identity.main() == 1
    assert probe.call_args.kwargs == {"parent_pid": None, "expected_identity": None}
