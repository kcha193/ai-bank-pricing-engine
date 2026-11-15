"""The operator review CLI: listing, showing and authenticated approve / reject / void."""

from __future__ import annotations

import hashlib
import logging
import secrets

import pytest
from quarantine_support import TD, spike_market

from balance_pricing_engine.pricing_intelligence import review_auth
from balance_pricing_engine.pricing_intelligence.cli import main
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

# Generated per run: no credential-shaped literal lives in the repository.
TOKEN = secrets.token_urlsafe(24)
OTHER_TOKEN = secrets.token_urlsafe(24)
WRONG_TOKEN = secrets.token_urlsafe(24)
EVIDENCE = "https://rates.example.test/asb/rate-card-2026-11-12.pdf"
CREDENTIALS = (
    f"alice={hashlib.sha256(TOKEN.encode()).hexdigest()},"
    f"bob={hashlib.sha256(OTHER_TOKEN.encode()).hexdigest()}"
)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "observations.db"
    market = spike_market(store=ObservationStore(path))
    market.store.close()
    return path


@pytest.fixture
def authorised(monkeypatch):
    monkeypatch.setenv(review_auth.CREDENTIALS_ENV_VAR, CREDENTIALS)
    monkeypatch.setenv(review_auth.TOKEN_ENV_VAR, TOKEN)


def cli(database, *args):
    return main(["--database", str(database), "--product", "term_deposit", *args])


def ledger(database):
    with ObservationStore(database) as store:
        return [dict(r) for r in store._connection.execute("SELECT * FROM quarantine_reviews")]


def pending_id(database) -> int:
    with ObservationStore(database) as store:
        (case,) = store.list_review_cases(TD)
    return case.event_id


# --------------------------------------------------------------------- list/show


def test_review_list_shows_what_an_operator_needs_to_decide(database, capsys):
    assert cli(database, "review", "list", "--status", "PENDING") == 0
    out = capsys.readouterr().out
    for expected in ("ASB", "term_deposit", "12", "4.00", "4.90", "+90", "PENDING", "run-2"):
        assert expected in out
    assert "age" in out.lower()


def test_review_list_is_empty_when_nothing_is_pending(tmp_path, capsys):
    path = tmp_path / "empty.db"
    ObservationStore(path).close()
    assert cli(path, "review", "list") == 0
    assert "No quarantined readings" in capsys.readouterr().out


def test_review_show_includes_evidence_signals_and_history(database, capsys, authorised):
    event_id = pending_id(database)
    assert cli(database, "review", "show", "--event-id", str(event_id)) == 0
    out = capsys.readouterr().out
    assert "suspicious_movement" in out and "rates.example.test" in out
    assert "History: none" in out or "No review decisions" in out
    cli(database, "review", "reject", "--event-id", str(event_id), "--reason", "misread")
    capsys.readouterr()
    cli(database, "review", "show", "--event-id", str(event_id))
    shown = capsys.readouterr().out
    assert "REJECT" in shown and "alice" in shown and "misread" in shown


def test_untrusted_source_text_cannot_inject_terminal_control_codes(database, capsys):
    with ObservationStore(database) as store:
        store._connection.execute(
            "UPDATE observations SET source_text = ? WHERE validation_status = 'warning'",
            ("ASB 12 months\x1b[31m 4.90%\x07",),
        )
        store._connection.commit()
    cli(database, "review", "show", "--event-id", str(pending_id(database)))
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out


# -------------------------------------------------------------- authorisation


def test_t02_unauthorised_approval_is_rejected(database, capsys, monkeypatch):
    for key in (review_auth.CREDENTIALS_ENV_VAR, review_auth.TOKEN_ENV_VAR):
        monkeypatch.delenv(key, raising=False)
    code = cli(
        database,
        "review", "approve", "--event-id", str(pending_id(database)),
        "--reason", "looks right", "--evidence", EVIDENCE,
    )  # fmt: skip
    assert code == 1
    assert "not configured" in capsys.readouterr().out
    assert ledger(database) == []


def test_wrong_token_is_rejected_without_echoing_secrets(database, capsys, monkeypatch):
    monkeypatch.setenv(review_auth.CREDENTIALS_ENV_VAR, CREDENTIALS)
    monkeypatch.setenv(review_auth.TOKEN_ENV_VAR, WRONG_TOKEN)
    code = cli(
        database,
        "review", "approve", "--event-id", str(pending_id(database)),
        "--reason", "x", "--evidence", EVIDENCE,
    )  # fmt: skip
    out = capsys.readouterr()
    assert code == 1 and "authorisation failed" in out.out
    for secret in (TOKEN, OTHER_TOKEN, WRONG_TOKEN, hashlib.sha256(TOKEN.encode()).hexdigest()):
        assert secret not in out.out + out.err
    assert ledger(database) == []


def test_short_or_missing_presented_token_is_refused(database, capsys, monkeypatch):
    monkeypatch.setenv(review_auth.CREDENTIALS_ENV_VAR, CREDENTIALS)
    monkeypatch.setenv(review_auth.TOKEN_ENV_VAR, "short")
    args = ("review", "reject", "--event-id", str(pending_id(database)), "--reason", "x")
    assert cli(database, *args) == 1
    monkeypatch.delenv(review_auth.TOKEN_ENV_VAR)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli(database, *args) == 1
    assert ledger(database) == []


def test_malformed_credentials_fail_closed(database, monkeypatch, capsys):
    monkeypatch.setenv(review_auth.TOKEN_ENV_VAR, TOKEN)
    for bad in ("alice", "alice=notahash", "=abc", ""):
        monkeypatch.setenv(review_auth.CREDENTIALS_ENV_VAR, bad)
        code = cli(
            database, "review", "reject", "--event-id", str(pending_id(database)), "--reason", "x"
        )
        assert code == 1
    assert ledger(database) == []


def test_no_token_option_exists_so_secrets_never_ride_on_the_command_line(database):
    with pytest.raises(SystemExit):
        cli(database, "review", "approve", "--event-id", "1", "--reason", "x", "--token", TOKEN)


def test_token_is_never_logged(database, authorised, caplog):
    caplog.set_level(logging.DEBUG)
    cli(database, "--verbose", "review", "reject", "--event-id", str(pending_id(database)), "--reason", "x")
    assert TOKEN not in caplog.text


def test_the_authenticated_identity_is_what_gets_recorded(database, monkeypatch, capsys):
    monkeypatch.setenv(review_auth.CREDENTIALS_ENV_VAR, CREDENTIALS)
    monkeypatch.setenv(review_auth.TOKEN_ENV_VAR, OTHER_TOKEN)
    assert cli(database, "review", "reject", "--event-id", str(pending_id(database)), "--reason", "misread") == 0
    (row,) = ledger(database)
    assert row["reviewer"] == "bob"


# ------------------------------------------------------------------ mutations


def test_t03_authenticated_approval_with_evidence_succeeds(database, authorised, capsys):
    event_id = pending_id(database)
    code = cli(
        database,
        "review", "approve", "--event-id", str(event_id),
        "--reason", "Verified against official rate card", "--evidence", EVIDENCE,
    )  # fmt: skip
    assert code == 0
    assert "APPROVED" in capsys.readouterr().out
    (row,) = ledger(database)
    assert (row["action"], row["reviewer"], row["evidence_ref"]) == ("APPROVE", "alice", EVIDENCE)
    with ObservationStore(database) as store:
        assert store.list_review_cases(TD) == []


def test_t04_approval_without_a_reason_is_rejected(database, authorised, capsys):
    code = cli(
        database,
        "review", "approve", "--event-id", str(pending_id(database)),
        "--reason", "  ", "--evidence", EVIDENCE,
    )  # fmt: skip
    assert code == 1 and "reason" in capsys.readouterr().out
    assert ledger(database) == []


def test_approval_without_evidence_is_rejected(database, authorised, capsys):
    code = cli(
        database, "review", "approve", "--event-id", str(pending_id(database)), "--reason", "ok"
    )
    assert code == 1 and "evidence" in capsys.readouterr().out


def test_t05_invalid_event_id_is_rejected(database, authorised, capsys):
    assert cli(database, "review", "reject", "--event-id", "424242", "--reason", "x") == 1
    assert "424242" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli(database, "review", "reject", "--event-id", "abc", "--reason", "x")


def test_t06_repeating_the_same_command_is_idempotent(database, authorised, capsys):
    args = ("review", "reject", "--event-id", str(pending_id(database)), "--reason", "misread")
    assert cli(database, *args) == 0
    capsys.readouterr()
    assert cli(database, *args) == 0
    assert "already" in capsys.readouterr().out.lower()
    assert len(ledger(database)) == 1


def test_t07_conflicting_action_is_rejected(database, authorised, capsys):
    event_id = str(pending_id(database))
    assert cli(database, "review", "reject", "--event-id", event_id, "--reason", "misread") == 0
    capsys.readouterr()
    code = cli(
        database, "review", "approve", "--event-id", event_id, "--reason", "changed mind",
        "--evidence", EVIDENCE,
    )  # fmt: skip
    assert code == 1 and "REJECTED" in capsys.readouterr().out
    assert [r["action"] for r in ledger(database)] == ["REJECT"]


def test_dry_run_previews_without_authorisation_or_writes(database, monkeypatch, capsys):
    for key in (review_auth.CREDENTIALS_ENV_VAR, review_auth.TOKEN_ENV_VAR):
        monkeypatch.delenv(key, raising=False)
    code = cli(
        database,
        "review", "approve", "--event-id", str(pending_id(database)),
        "--reason", "preview", "--evidence", EVIDENCE, "--dry-run",
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0 and "DRY RUN" in out and "APPROVED" in out
    assert ledger(database) == []


def test_void_is_available_for_a_previously_accepted_reading(tmp_path, authorised, capsys):
    from quarantine_support import Market
    from test_pricing_intelligence_quarantine_review import no_peer_check

    path = tmp_path / "accepted.db"
    market = Market(TD, store=ObservationStore(path), **no_peer_check())
    market.run(0)
    market.run(1)
    market.set("ASB", 4.60)
    market.run(2)
    market.store.close()
    assert cli(path, "review", "list", "--status", "ACCEPTED") == 0
    assert "4.60" in capsys.readouterr().out
    with ObservationStore(path) as store:
        from balance_pricing_engine.pricing_intelligence.quarantine import ReviewState

        (case,) = store.list_review_cases(TD, [ReviewState.ACCEPTED])
    assert cli(path, "review", "void", "--event-id", str(case.event_id), "--reason", "parser error") == 0
    assert [r["action"] for r in ledger(path)] == ["VOID"]


# --------------------------------------------------------------- auth module


def test_review_auth_accepts_only_a_matching_digest_and_returns_the_identity():
    env = {review_auth.CREDENTIALS_ENV_VAR: CREDENTIALS, review_auth.TOKEN_ENV_VAR: TOKEN}
    assert review_auth.authenticate(env).identity == "alice"
    env[review_auth.TOKEN_ENV_VAR] = WRONG_TOKEN
    with pytest.raises(review_auth.ReviewAuthError):
        review_auth.authenticate(env)


def test_review_auth_does_not_leak_secrets_in_its_error_or_repr():
    env = {review_auth.CREDENTIALS_ENV_VAR: CREDENTIALS, review_auth.TOKEN_ENV_VAR: WRONG_TOKEN}
    with pytest.raises(review_auth.ReviewAuthError) as caught:
        review_auth.authenticate(env)
    assert WRONG_TOKEN not in str(caught.value) and hashlib.sha256(TOKEN.encode()).hexdigest() not in str(
        caught.value
    )
    env[review_auth.TOKEN_ENV_VAR] = TOKEN
    assert TOKEN not in repr(review_auth.authenticate(env))


# ------------------------------------------------------------------- health


def test_t23_health_command_reports_the_pending_quarantine(database, capsys):
    cli(database, "health")
    out = capsys.readouterr().out
    assert "Quarantine: 1 reading(s) awaiting review" in out and "ASB" in out
    assert "ESCALATED" not in out


def test_health_command_reports_an_escalation_and_a_clean_state(tmp_path, capsys):
    path = tmp_path / "escalated.db"
    market = spike_market(store=ObservationStore(path))
    market.run(3)
    market.run(4)
    market.store.close()
    cli(path, "health")
    assert "ESCALATED (1 at or beyond 3 runs)" in capsys.readouterr().out
    clean = tmp_path / "clean.db"
    ObservationStore(clean).close()
    cli(clean, "health")
    assert "no readings awaiting review" in capsys.readouterr().out


def test_review_list_all_statuses_and_empty_filter(database, capsys, authorised):
    cli(database, "review", "reject", "--event-id", str(pending_id(database)), "--reason", "misread")
    capsys.readouterr()
    assert cli(database, "review", "list", "--status", "ALL") == 0
    assert "REJECTED" in capsys.readouterr().out
    assert cli(database, "review", "list") == 0
    assert "No quarantined readings" in capsys.readouterr().out
