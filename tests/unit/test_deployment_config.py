"""Validates the deployment configuration files without requiring a Docker daemon."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_exists_and_declares_a_non_root_user():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "USER pricing" in dockerfile
    assert "useradd" in dockerfile


def test_dockerfile_declares_healthcheck_and_exposes_port():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "HEALTHCHECK" in dockerfile
    assert "/health" in dockerfile
    assert "EXPOSE 8000" in dockerfile


def test_dockerfile_healthcheck_honours_configured_port():
    """server.py listens on PRICING_ENGINE_API_PORT (default 8000, see
    api/server.py::main). A HEALTHCHECK hardcoded to localhost:8000 would mark a
    container started with a non-default port unhealthy even while it serves fine."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    healthcheck_line = next(
        line for line in dockerfile.splitlines() if "curl" in line and "/health" in line
    )

    assert "PRICING_ENGINE_API_PORT" in healthcheck_line
    assert "localhost:8000" not in healthcheck_line


def test_dockerfile_installs_api_extra_and_runs_the_api_server_launcher():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert '.[api]' in dockerfile
    assert "balance_pricing_engine.api.server" in dockerfile


def test_api_server_launcher_runs_uvicorn_against_the_app():
    server_module = (
        REPO_ROOT / "src" / "balance_pricing_engine" / "api" / "server.py"
    ).read_text(encoding="utf-8")

    assert "uvicorn" in server_module
    assert "balance_pricing_engine.api.app:app" in server_module


def test_dockerignore_excludes_dev_and_data_directories():
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    for entry in [".git/", "tests/", "data/outputs/", ".venv/", "__pycache__/"]:
        assert entry in dockerignore


def test_deployment_doc_covers_required_topics():
    deployment_doc = (REPO_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    for heading in [
        "Local API execution",
        "Docker execution",
        "Health and readiness",
        "Configuration handling",
        "Persistence considerations",
        "Production boundaries",
    ]:
        assert heading in deployment_doc
