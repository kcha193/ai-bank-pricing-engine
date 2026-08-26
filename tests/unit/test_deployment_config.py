"""Validates the deployment configuration files without requiring a Docker daemon."""

import plistlib
from pathlib import Path

import yaml

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


def test_deployment_doc_lists_both_secrets_read_by_the_service():
    """PRICING_ENGINE_API_KEYS is a second secret read through SecretsProvider (via
    require_api_key), alongside ANTHROPIC_API_KEY -- the deployment inventory must list
    both, not just the LLM key, so operators migrating secrets to Vault/etc. don't miss
    the auth credential."""
    deployment_doc = (REPO_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    assert "ANTHROPIC_API_KEY" in deployment_doc
    assert "PRICING_ENGINE_API_KEYS" in deployment_doc
    assert "SecretsProvider" in deployment_doc


def test_launchd_plist_is_well_formed_xml():
    """A stray "--" anywhere inside an XML comment makes the whole file
    unparseable (XML forbids double hyphens in comment content), so macOS
    rejects it before launchd can even install the scheduled collector --
    plutil is lenient about this, but plistlib (and launchd itself) is not."""
    plist_path = REPO_ROOT / "deploy" / "scheduling" / "nz.pricing-intelligence.collect.plist"

    with plist_path.open("rb") as handle:
        data = plistlib.load(handle)

    assert data["Label"] == "nz.pricing-intelligence.collect"


def test_scheduling_recipes_pass_config_flag():
    """Without --config, collect_market_rates_workflow.py falls back to
    PricingIntelligenceConfig() defaults and silently ignores any
    pricing_intelligence block (selected banks, validation thresholds,
    collection delays, database path) an operator configured in
    configs/base.yaml -- the ready-made recipes must actually wire it up,
    not just support it."""
    plist_path = REPO_ROOT / "deploy" / "scheduling" / "nz.pricing-intelligence.collect.plist"
    with plist_path.open("rb") as handle:
        program_arguments = plistlib.load(handle)["ProgramArguments"]
    assert "--config" in program_arguments

    crontab_example = (
        REPO_ROOT / "deploy" / "scheduling" / "crontab.example"
    ).read_text(encoding="utf-8")
    assert "--config" in crontab_example


def test_collector_canary_cron_targets_nz_weekdays():
    """GitHub evaluates schedule cron in UTC only, with no IANA timezone
    support. NZST (UTC+12) puts NZ local Monday 07:30 at UTC Sunday 19:30 --
    the previous UTC calendar day -- so the UTC day-of-week field must be
    Sun-Thu (0-4) to land on NZ local Mon-Fri. Mon-Fri (1-5) in UTC actually
    runs NZ local Tue-Sat, skipping the Monday-morning canary entirely."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "collector-canary.yml").read_text(
            encoding="utf-8"
        )
    )
    # PyYAML implements YAML 1.1, where the bare `on:` trigger key parses as
    # the boolean True rather than the string "on" -- check both so this
    # isn't tied to that quirk.
    triggers = workflow.get("on", workflow.get(True))

    cron = triggers["schedule"][0]["cron"]

    assert cron == "30 19 * * 0-4"


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
