"""Phase 2a replay of audit challenge case Q (Q6 / Q7) on the corrected code.

Reuses the preserved offline harness from ``2026-11-13-evidence/ingest/harness.py`` and the same
fixture pages, business clock and run ids as ``case_Q_storage.py``; neither is modified. Run:

    PYTHONPATH=src python docs/audits/2026-11-15-phase-2a-evidence/case_Q_after.py

Offline: no network, temporary SQLite databases, no alert sinks, no real credentials.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "2026-11-13-evidence" / "ingest"))
from harness import *  # noqa: E402,F403  (make_service, url_map, T0, TD_FIXTURE, BANKS, ...)

from balance_pricing_engine.competitor_rates.collectors import build_collectors  # noqa: E402
from balance_pricing_engine.pricing_intelligence.config import (  # noqa: E402
    PeerPlausibilityThresholds,
    ValidationThresholds,
)
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle  # noqa: E402
from balance_pricing_engine.pricing_intelligence.quarantine import ReviewAction, ReviewState  # noqa: E402
from balance_pricing_engine.pricing_intelligence.strategy import resolve_competitor_rate  # noqa: E402

P = "term_deposit"


def asb_with(rate: str):
    html = TD_FIXTURE["ASB"].read_text()
    return html.replace("<tr><td>12 Months</td><td>4.00% p.a.</td></tr>",
                        f"<tr><td>12 Months</td><td>{rate}% p.a.</td></tr>")


def swap(svc, mapping):
    svc._collectors = build_collectors(banks=BANKS, config=svc.config.collection,
                                       fetcher=make_fetcher(mapping), raw_root=svc._raw_root)


def asb12(svc, now):
    t = svc.get_market_snapshot(product_type=P, now=now).term(12)
    e = [x for x in (t.entries if t else ()) if x.bank == "ASB"]
    return (e[0].rate_pct, e[0].quarantined) if e else None


def leader(svc, now):
    return resolve_competitor_rate("MARKET_LEADER", svc.get_market_snapshot(product_type=P, now=now), 12).rate_pct


def cycle(svc, day, run_id):
    return run_operational_cycle(svc, product_type=P, captured_at_utc=T0 + timedelta(days=day),
                                 run_id=run_id, alert_sinks={})


def baseline(**config):
    svc, _ = make_service(**config)
    cycle(svc, 0, "run-A")
    swap(svc, url_map({"ASB:term_deposit": asb_with("3.90")}))
    cycle(svc, 1, "run-B")  # Q4's genuine -10 bps
    return svc


print("Q6 plausible-but-wrong +60 bps (3.90 -> 4.50), peer check ON (default)")
svc = baseline()
swap(svc, url_map({"ASB:term_deposit": asb_with("4.50")}))
r = cycle(svc, 2, "run-G")
now = T0 + timedelta(days=2, hours=1)
print("  changes", [(c.describe(), "suspicious" if c.suspicious else "accepted") for c in r.outcome.changes], "code", r.code)
print("  peer assessments", [(a.bank, a.status, a.peer_count, a.peer_median_pct, round(a.deviation_bps, 1), round(a.excess_movement_bps, 1))
                             for a in r.outcome.validation.peer_assessments if a.status != "ok" or a.bank == "ASB"])
print("  ASB12 in trusted snapshot (rate, quarantined):", asb12(svc, now), " MARKET_LEADER", leader(svc, now))
(case,) = svc.list_review_cases(P, now=now)
print("  review queue:", (case.event_id, case.bank, case.previous_rate_pct, case.proposed_rate_pct, case.status.value, list(case.signals)[:1]))

print("Q6b the same +60 bps reading with the peer signal OFF (the audit's unfixed acceptance), then VOID")
off = ValidationThresholds(peer=PeerPlausibilityThresholds(enabled=False))
svc = baseline(validation=off)
swap(svc, url_map({"ASB:term_deposit": asb_with("4.50")}))
r = cycle(svc, 2, "run-G")
now = T0 + timedelta(days=2, hours=1)
print("  accepted unflagged; changes", [(c.describe(), c.suspicious) for c in r.outcome.changes], "MARKET_LEADER", leader(svc, now))
(acc,) = [c for c in svc.list_review_cases(P, [ReviewState.ACCEPTED], now=now) if c.proposed_rate_pct == 4.5]
out = svc.review(acc.event_id, ReviewAction.VOID, reviewer="ops-lead", reason="source parsing error",
                 reviewed_at=T0 + timedelta(days=2, hours=2))
print("  void recorded:", out.event.action.value, out.event.previous_state.value, "->", out.event.new_state.value)
after = T0 + timedelta(days=2, hours=3)
print("  after void: ASB12", asb12(svc, after), "MARKET_LEADER", leader(svc, after),
      "activity", svc.get_market_activity(now=after, product_type=P).changes, "(only the genuine -10 bps remains)")

print("Q7 genuine +100 bps (3.90 -> 4.90), re-observed daily, UNAPPROVED (threshold 3 runs)")
svc = baseline()
swap(svc, url_map({"ASB:term_deposit": asb_with("4.90")}))
for day in range(2, 9):
    r = cycle(svc, day, f"run-big-{day}")
    now = T0 + timedelta(days=day, hours=1)
    q = r.quarantine
    print(f"  day {day}: code {r.code} status {r.status} ASB12={asb12(svc, now)} MARKET_LEADER={leader(svc, now)} "
          f"pending={q.pending_count} escalated={len(q.escalated)} runs_pending={q.pending[0].runs_pending}")

print("Q7b the same, APPROVED on day 3 with evidence")
svc = baseline()
swap(svc, url_map({"ASB:term_deposit": asb_with("4.90")}))
cycle(svc, 2, "run-big-2")
cycle(svc, 3, "run-big-3")
(case,) = svc.list_review_cases(P, now=T0 + timedelta(days=3, hours=1))
out = svc.review(case.event_id, ReviewAction.APPROVE, reviewer="ops-lead",
                 reason="Confirmed on the published rate card",
                 evidence_ref="https://rates.example.test/asb/term-deposit-rate-card.pdf",
                 reviewed_at=T0 + timedelta(days=3, hours=2))
now = T0 + timedelta(days=3, hours=3)
print("  approved:", out.event.action.value, out.event.previous_state.value, "->", out.event.new_state.value,
      "| ASB12", asb12(svc, now), "MARKET_LEADER", leader(svc, now))
r = cycle(svc, 4, "run-big-4")
print("  next run: code", r.code, "new changes", len(r.outcome.changes), "pending", r.quarantine.pending_count)
print("  rate-change history (ASB 12m):", [(c.previous_rate_pct, c.current_rate_pct, "suspicious" if c.suspicious else "trusted")
                                         for c in svc.get_changes(days=None, now=now, product_type=P) if c.is_rate_move])
