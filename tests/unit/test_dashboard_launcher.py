"""The double-click launcher (``Pricing Watch.command``): private, repo-relative, predictable.

Everything runs in dry-run mode (it resolves and checks, then prints what it would do), so no
server starts and no browser opens. The launcher is plain bash, so these tests need only bash.
"""

import http.server
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "Pricing Watch.command"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

INSTALL_HINT = 'pip install -e ".[dashboard]"'


@pytest.fixture
def repo(tmp_path):
    """A stand-in checkout: the launcher copied in, an app file, and a stub interpreter.

    The stub answers the launcher's ``import streamlit`` check itself (so tests do not depend on
    Streamlit being installed) and defers everything else to the real interpreter.
    """
    root = tmp_path / "checkout"
    (root / "src/balance_pricing_engine/dashboard").mkdir(parents=True)
    (root / "src/balance_pricing_engine/dashboard/app.py").write_text("# app\n")
    shutil.copy(LAUNCHER, root / LAUNCHER.name)
    (root / LAUNCHER.name).chmod(0o755)
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        '#!/bin/sh\nif [ "$1" = "-c" ] && [ "$2" = "import streamlit" ]; then exit 0; fi\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    python.chmod(0o755)
    return root


def launch(script, *, env=None, cwd=None):
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("PRICING_WATCH")}
    run_env.update({"PRICING_WATCH_LAUNCHER_DRY_RUN": "1", "PRICING_WATCH_NO_BROWSER": "1"})
    run_env.update(env or {})
    return subprocess.run(
        [BASH, str(script)],
        capture_output=True,
        text=True,
        env=run_env,
        cwd=cwd,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def facts(result):
    """The ``DRY_RUN key=value`` lines the launcher prints, as a dict."""
    found = {}
    for line in result.stdout.splitlines():
        if line.startswith("DRY_RUN "):
            key, _, value = line[len("DRY_RUN ") :].partition("=")
            found[key] = value
    return found


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- the file itself ---------------------------------------------------------------------------


def test_the_launcher_is_a_bash_script_that_can_be_double_clicked():
    assert LAUNCHER.is_file()
    assert LAUNCHER.read_text().startswith("#!/bin/bash\n")
    assert os.access(LAUNCHER, os.X_OK), "needs the executable bit for Finder to run it"


def test_git_records_the_executable_bit():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    listing = subprocess.run(
        ["git", "ls-files", "-s", "--", LAUNCHER.name], cwd=ROOT, capture_output=True, text=True
    ).stdout
    assert listing.startswith("100755 "), f"not tracked as executable: {listing!r}"


def test_the_launcher_never_binds_beyond_localhost():
    text = LAUNCHER.read_text()
    assert "0.0.0.0" not in text
    assert "--server.address 127.0.0.1" in text


def test_dot_env_is_git_ignored_but_the_example_is_not():
    if shutil.which("git") is None:
        pytest.skip("git not available")

    def ignored(name):
        return (
            subprocess.run(
                ["git", "check-ignore", "-q", name], cwd=ROOT, capture_output=True
            ).returncode
            == 0
        )

    assert ignored(".env") and ignored(".env.local")
    assert not ignored(".env.example")


# --- runs from the repo it lives in --------------------------------------------------------------


def test_it_runs_from_the_repo_it_lives_in_whatever_the_working_directory(repo, tmp_path):
    elsewhere = tmp_path / "somewhere" / "else"
    elsewhere.mkdir(parents=True)
    result = launch(repo / LAUNCHER.name, cwd=elsewhere)
    assert result.returncode == 0, result.stdout + result.stderr
    found = facts(result)
    assert Path(found["repo"]).resolve() == repo.resolve()
    assert (
        Path(found["app"]).resolve()
        == (repo / "src/balance_pricing_engine/dashboard/app.py").resolve()
    )
    assert Path(found["pythonpath"].split(":")[0]).resolve() == (repo / "src").resolve()


def test_it_resolves_symlinks_back_to_the_real_repo(repo, tmp_path):
    link_dir = tmp_path / "desktop"
    link_dir.mkdir()
    link = link_dir / "Pricing Watch.command"
    link.symlink_to(repo / LAUNCHER.name)
    result = launch(link, cwd=link_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(facts(result)["repo"]).resolve() == repo.resolve()


def test_it_prefers_the_repos_own_virtualenv(repo):
    found = facts(launch(repo / LAUNCHER.name))
    assert Path(found["python"]).resolve() == (repo / ".venv/bin/python").resolve()


def test_an_explicit_python_override_wins(repo, tmp_path):
    other = tmp_path / "other-python"
    other.write_text(
        '#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\nexec "' + sys.executable + '" "$@"\n'
    )
    other.chmod(0o755)
    found = facts(launch(repo / LAUNCHER.name, env={"PRICING_WATCH_PYTHON": str(other)}))
    assert found["python"] == str(other)


def test_a_folder_that_is_not_the_repo_gets_a_clear_error(tmp_path):
    lonely = tmp_path / "not-a-repo"
    lonely.mkdir()
    shutil.copy(LAUNCHER, lonely / LAUNCHER.name)
    result = launch(lonely / LAUNCHER.name)
    assert result.returncode != 0
    assert "dashboard/app.py" in result.stdout + result.stderr


# --- a missing install is explained, not a stack trace -------------------------------------------


def test_missing_streamlit_explains_how_to_install_it(repo, tmp_path):
    broken = tmp_path / "no-streamlit-python"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    result = launch(repo / LAUNCHER.name, env={"PRICING_WATCH_PYTHON": str(broken)})
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert INSTALL_HINT in output
    assert "Traceback" not in output


# --- private by default: localhost, a predictable port, no second server --------------------------


def test_it_serves_on_localhost_only_on_the_default_port(repo):
    found = facts(launch(repo / LAUNCHER.name))
    assert found["address"] == "127.0.0.1"
    assert found["port"] == "8765"
    assert found["already_running"] == "0"
    assert "--server.address 127.0.0.1" in found["command"]
    assert "--server.headless true" in found["command"]
    assert "--browser.gatherUsageStats false" in found["command"]
    assert "0.0.0.0" not in found["command"]


def test_the_port_can_be_chosen(repo):
    port = free_port()
    found = facts(launch(repo / LAUNCHER.name, env={"PRICING_WATCH_PORT": str(port)}))
    assert found["port"] == str(port)


def test_a_busy_port_falls_through_to_the_next_free_one(repo):
    port = free_port()
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", port))
        blocker.listen()
        found = facts(launch(repo / LAUNCHER.name, env={"PRICING_WATCH_PORT": str(port)}))
    assert int(found["port"]) > port and found["already_running"] == "0"


@pytest.mark.parametrize("bad", ["abc", "0", "70000", "80 80"])
def test_a_nonsense_port_is_rejected_with_a_message(repo, bad):
    result = launch(repo / LAUNCHER.name, env={"PRICING_WATCH_PORT": bad})
    assert result.returncode != 0
    assert "PRICING_WATCH_PORT" in result.stdout + result.stderr


def test_a_running_dashboard_is_reused_not_started_twice(repo):
    class Healthy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            body = b"ok" if self.path == "/_stcore/health" else b"<html></html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Healthy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        found = facts(launch(repo / LAUNCHER.name, env={"PRICING_WATCH_PORT": str(port)}))
    finally:
        server.shutdown()
    assert found["already_running"] == "1"
    assert found["port"] == str(port)


# --- configuration from .env, without ever echoing a secret --------------------------------------


def test_dot_env_is_loaded_and_its_values_are_never_printed(repo):
    (repo / ".env").write_text(
        "# local settings\nPRICING_WATCH_OPS_TOKEN=correct-horse-battery-staple\n"
        'TELEGRAM_BOT_TOKEN="123456:SECRET-VALUE"\n'
    )
    result = launch(repo / LAUNCHER.name)
    assert result.returncode == 0, result.stdout + result.stderr
    found = facts(result)
    assert found["env_file"] == "loaded"
    assert found["ops_token"] == "set"
    assert "correct-horse-battery-staple" not in result.stdout + result.stderr
    assert "SECRET-VALUE" not in result.stdout + result.stderr


def test_without_a_dot_env_the_launcher_still_runs_and_says_so(repo):
    found = facts(launch(repo / LAUNCHER.name))
    assert found["env_file"] == "none" and found["ops_token"] == "unset"


def test_the_ops_token_can_also_come_from_the_environment(repo):
    found = facts(launch(repo / LAUNCHER.name, env={"PRICING_WATCH_OPS_TOKEN": "x" * 24}))
    assert found["ops_token"] == "set"
