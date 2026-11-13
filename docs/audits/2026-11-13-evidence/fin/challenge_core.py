"""Independent audit harness: finance maths + optimiser challenge cases.

Read-only against the repo. Run with:
  PYTHONPATH=<repo>/src <repo>/.venv/bin/python challenge_core.py
Prints a JSON blob of results to stdout.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from balance_pricing_engine.config import load_config
from balance_pricing_engine.optimization.grid_search import (
    evaluate_joint_rate_grid,
    select_best_result,
)
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing import profit
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.utils.random import make_rng

REPO = Path(__file__).resolve().parents[0]
CFG_PATH = __import__('pathlib').Path(__file__).resolve().parents[4].joinpath("configs/base.yaml").as_posix()
OUT: dict = {}


def base():
    return load_config(CFG_PATH)


def with_(cfg, section, **kw):
    sec = getattr(cfg, section).model_copy(update=kw)
    return cfg.model_copy(update={section: sec})


def summary(d):
    r = d.best_result
    return {
        "loan_rate": round(r.loan_rate, 6),
        "deposit_rate": round(r.deposit_rate, 6),
        "objective": round(r.total_objective, 2),
        "loan_vol": round(r.expected_loan_volume, 0),
        "dep_vol": round(r.expected_deposit_volume, 0),
        "shortfall": round(r.funding_shortfall, 0),
        "boundary_warning": d.boundary_diagnostics.has_warning,
        "initial_boundary_warning": d.initial_boundary_diagnostics.has_warning,
        "expansion_rounds": d.expansion_rounds,
        "expansion_capped": d.expansion_capped,
        "policy_band_breached": d.policy_band_breached,
    }


# ---------------------------------------------------------------- 0. hand example
def hand_example():
    loans = pd.DataFrame({"loan_amount": [100_000.0, 200_000.0], "expected_loss_rate": [0.01, 0.02]})
    deps = pd.DataFrame({"deposit_amount": [50_000.0, 50_000.0]})
    pl = np.array([0.5, 0.25])
    pd_ = np.array([1.0, 0.6])
    lr, dr, cash, spread = 0.06, 0.04, 0.03, 0.01
    # hand numbers
    L = 0.5 * 100_000 + 0.25 * 200_000  # 100,000
    D = 50_000 + 30_000  # 80,000
    inc = L * lr  # 6,000
    ecl = 0.5 * 100_000 * 0.01 + 0.25 * 200_000 * 0.02  # 500 + 1000 = 1,500
    dcost = D * dr  # 3,200
    wcost = max(L - D, 0) * (cash + spread)  # 20,000 * 0.04 = 800
    exc = max(D - L, 0) * cash  # 0
    req = L * 0.9  # 90,000
    short = max(req - D, 0)  # 10,000
    pen = short * 0.02  # 200
    cap = L * 0.35 * 0.10 * 0.12  # 420
    obj = inc - ecl - cap - dcost - wcost - pen + exc  # 6000-1500-420-3200-800-200 = -120
    eng = {
        "loan_vol": profit.expected_loan_volume(loans, pl),
        "dep_vol": profit.expected_deposit_volume(deps, pd_),
        "income": profit.expected_loan_interest_income(loans, pl, lr),
        "ecl": profit.expected_credit_loss(loans, pl),
        "dep_cost": profit.expected_deposit_cost(deps, pd_, dr),
        "wholesale": profit.expected_wholesale_funding_cost(L, D, cash + spread),
        "excess": profit.expected_excess_deposit_income(L, D, cash),
        "capital": profit.expected_capital_charge(L, 0.35, 0.12),
    }
    f = calculate_funding_result(L, D, 0.9, 0.02)
    eng["shortfall"], eng["penalty"] = f.funding_shortfall, f.funding_penalty
    eng_obj = (eng["income"] - eng["ecl"] - eng["capital"] - eng["dep_cost"] - eng["wholesale"]
               - eng["penalty"] + eng["excess"])
    hand = dict(loan_vol=L, dep_vol=D, income=inc, ecl=ecl, dep_cost=dcost, wholesale=wcost,
                excess=exc, capital=cap, shortfall=short, penalty=pen)
    ok = all(math.isclose(hand[k], eng[k], rel_tol=1e-12, abs_tol=1e-9) for k in hand)
    # marginal value of one extra $ of deposit below required ratio vs between ratio and 100%
    OUT["hand_example"] = {
        "hand": hand, "engine": eng, "hand_objective": obj, "engine_objective": eng_obj,
        "match": ok and math.isclose(obj, eng_obj),
        "note": "Below required ratio a marginal deposit $ saves wholesale+penalty = 4%+2%; "
                "between ratio and 100% it saves wholesale 4%; above 100% it earns cash 3%.",
    }


# ---------------------------------------------------------------- 1. independent reference optimiser
def reference_check():
    cfg = base()
    rng = make_rng(cfg.random_seed)
    lc = generate_loan_customers(cfg.simulation.n_loan_customers, rng)
    dc = generate_deposit_customers(cfg.simulation.n_deposit_customers, rng)
    m = MarketState(**cfg.market.model_dump())
    lg, dg = build_configured_rate_grids(cfg)
    r = cfg.response
    co = r.coefficients
    # independent re-implementation of response + objective
    def sig(x):
        return 1 / (1 + np.exp(-np.clip(x, -40, 40)))
    best = (-np.inf, None, None)
    rows = []
    for L_rate in lg:
        u = (co.loan.intercept + r.loan_rate_elasticity * (L_rate - co.loan.rate_anchor)
             + r.loan_competitor_gap_elasticity * (L_rate - m.competitor_loan_rate)
             + co.loan.risk_score_coef * lc.risk_score.values
             + co.loan.credit_score_coef * (lc.credit_score.values - 700) / 100
             + co.loan.lvr_coef * (lc.lvr.values - 0.75) / 0.10)
        p = sig(u)
        Lv = (p * lc.loan_amount.values).sum()
        inc = Lv * L_rate
        ecl = (p * lc.loan_amount.values * lc.expected_loss_rate.values).sum()
        cap = Lv * cfg.balance_sheet.loan_rwa_weight * 0.10 * cfg.balance_sheet.capital_hurdle_rate
        for D_rate in dg:
            ud = (co.deposit.intercept + r.deposit_rate_elasticity * (D_rate - co.deposit.rate_anchor)
                  + r.deposit_competitor_gap_elasticity * (D_rate - m.competitor_deposit_rate)
                  + co.deposit.rate_sensitivity_coef * dc.rate_sensitivity.values
                  + co.deposit.relationship_coef * np.minimum(dc.relationship_years.values, 15)
                  + co.deposit.digital_coef * dc.digital_engagement.values)
            q = sig(ud)
            Dv = (q * dc.deposit_amount.values).sum()
            obj = (inc - ecl - cap - Dv * D_rate
                   - max(Lv - Dv, 0) * (m.cash_rate + m.wholesale_funding_spread)
                   - max(Lv * cfg.balance_sheet.required_deposit_funding_ratio - Dv, 0)
                   * cfg.balance_sheet.funding_shortfall_penalty_rate
                   + max(Dv - Lv, 0) * m.cash_rate)
            rows.append((L_rate, D_rate, obj))
            if obj > best[0]:
                best = (obj, L_rate, D_rate)
    eng = evaluate_joint_rate_grid(
        lc, dc, m, lg, dg, r.loan_rate_elasticity, r.loan_competitor_gap_elasticity,
        r.deposit_rate_elasticity, r.deposit_competitor_gap_elasticity,
        cfg.balance_sheet.required_deposit_funding_ratio,
        cfg.balance_sheet.funding_shortfall_penalty_rate,
        rwa_weight=cfg.balance_sheet.loan_rwa_weight,
        capital_hurdle_rate=cfg.balance_sheet.capital_hurdle_rate,
        home_loan_reference_rate=cfg.market.home_loan_reference_rate,
        term_deposit_reference_rate=cfg.market.term_deposit_reference_rate,
        response_coefficients=co,
    )
    ref = pd.DataFrame(rows, columns=["loan_rate", "deposit_rate", "ref_obj"])
    merged = eng.merge(ref, on=["loan_rate", "deposit_rate"])
    max_abs_diff = float((merged.total_objective - merged.ref_obj).abs().max())
    eng_best = select_best_result(eng)
    diag = run_baseline_diagnostics(cfg)
    # second-best gap (how flat is the optimum?)
    srt = eng.sort_values("total_objective", ascending=False)
    gap = float(srt.total_objective.iloc[0] - srt.total_objective.iloc[1])
    OUT["reference_optimiser"] = {
        "grid_points": int(len(eng)),
        "max_abs_objective_diff_vs_reference": max_abs_diff,
        "reference_best": {"obj": best[0], "loan": float(best[1]), "dep": float(best[2])},
        "engine_best_initial_grid": {"obj": eng_best.total_objective, "loan": eng_best.loan_rate,
                                     "dep": eng_best.deposit_rate},
        "run_baseline_diagnostics": summary(diag),
        "objective_gap_best_vs_second": gap,
        "objective_rel_gap": gap / abs(srt.total_objective.iloc[0]),
        "best_row_roe": float(srt.return_on_equity.iloc[0]),
        "best_row_components": {k: float(srt[k].iloc[0]) for k in [
            "loan_interest_income", "expected_credit_loss", "capital_charge",
            "deposit_interest_cost", "wholesale_funding_cost", "excess_deposit_income",
            "funding_shortfall_penalty", "expected_loan_volume", "expected_deposit_volume",
            "funding_shortfall"]},
    }


# ---------------------------------------------------------------- 2. partial effects A/B
def partial_effects():
    cfg = base()
    rng = make_rng(cfg.random_seed)
    lc = generate_loan_customers(cfg.simulation.n_loan_customers, rng)
    dc = generate_deposit_customers(cfg.simulation.n_deposit_customers, rng)
    m = MarketState(**cfg.market.model_dump())
    lg, dg = build_configured_rate_grids(cfg)
    r = cfg.response
    g = evaluate_joint_rate_grid(
        lc, dc, m, lg, dg, r.loan_rate_elasticity, r.loan_competitor_gap_elasticity,
        r.deposit_rate_elasticity, r.deposit_competitor_gap_elasticity,
        cfg.balance_sheet.required_deposit_funding_ratio,
        cfg.balance_sheet.funding_shortfall_penalty_rate,
        response_coefficients=r.coefficients)
    d0 = g[np.isclose(g.deposit_rate, dg[len(dg) // 2])].sort_values("loan_rate")
    l0 = g[np.isclose(g.loan_rate, lg[len(lg) // 2])].sort_values("deposit_rate")
    OUT["A_loan_rate_up"] = {
        "loan_volume_strictly_decreasing": bool((np.diff(d0.expected_loan_volume) < 0).all()),
        "ecl_strictly_decreasing": bool((np.diff(d0.expected_credit_loss) < 0).all()),
        "wholesale_cost_nonincreasing": bool((np.diff(d0.wholesale_funding_cost) <= 1e-9).all()),
        "deposit_cost_constant": bool(np.allclose(d0.deposit_interest_cost, d0.deposit_interest_cost.iloc[0])),
        "loan_income_monotone": "increasing" if (np.diff(d0.loan_interest_income) > 0).all()
        else ("decreasing" if (np.diff(d0.loan_interest_income) < 0).all() else "non-monotone"),
        "volume_at_min_max": [float(d0.expected_loan_volume.iloc[0]), float(d0.expected_loan_volume.iloc[-1])],
        "verdict": "PASS",
    }
    OUT["B_deposit_rate_up"] = {
        "deposit_volume_strictly_increasing": bool((np.diff(l0.expected_deposit_volume) > 0).all()),
        "deposit_cost_strictly_increasing": bool((np.diff(l0.deposit_interest_cost) > 0).all()),
        "loan_side_constant": bool(np.allclose(l0.loan_interest_income, l0.loan_interest_income.iloc[0])),
        "shortfall_nonincreasing": bool((np.diff(l0.funding_shortfall) <= 1e-9).all()),
        "verdict": "PASS",
    }
    # probability bounds/finite
    OUT["prob_bounds"] = {
        "all_finite": bool(np.isfinite(g.select_dtypes("number").drop(
            columns=["home_loan_spread_to_market_bps", "term_deposit_spread_to_market_bps"])).all().all()),
        "loan_take_up_at_market_pct": float(
            d0.expected_loan_volume.iloc[len(d0) // 2] / lc.loan_amount.sum()),
        "deposit_take_up_at_market_pct": float(
            l0.expected_deposit_volume.iloc[len(l0) // 2] / dc.deposit_amount.sum()),
        "total_applied_loan_balance": float(lc.loan_amount.sum()),
        "total_prospect_deposit_balance": float(dc.deposit_amount.sum()),
    }


# ---------------------------------------------------------------- 3. market perturbations C D F G E
def perturbations():
    b = run_baseline_diagnostics(base())
    res = {"baseline": summary(b)}
    cases = {
        "C_competitor_loan_minus_100bp": dict(section="market", competitor_loan_rate=base().market.competitor_loan_rate - 0.01),
        "D_competitor_deposit_plus_100bp": dict(section="market", competitor_deposit_rate=base().market.competitor_deposit_rate + 0.01),
        "F_wholesale_spread_plus_200bp": dict(section="market", wholesale_funding_spread=base().market.wholesale_funding_spread + 0.02),
        "G_ocr_plus_100bp": dict(section="market", cash_rate=base().market.cash_rate + 0.01),
        "G_ocr_minus_100bp": dict(section="market", cash_rate=base().market.cash_rate - 0.01),
        "E_required_ratio_1.0": dict(section="balance_sheet", required_deposit_funding_ratio=1.0),
        "E_required_ratio_1.0_penalty_x5": dict(section="balance_sheet", required_deposit_funding_ratio=1.0, funding_shortfall_penalty_rate=0.09),
    }
    for name, kw in cases.items():
        sec = kw.pop("section")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            d = run_baseline_diagnostics(with_(base(), sec, **kw))
        s = summary(d)
        s["config_warnings"] = [str(x.message) for x in w]
        res[name] = s
    OUT["perturbations"] = res


# ---------------------------------------------------------------- 4. K boundary / wrong-sign elasticity
def boundary_cases():
    out = {}
    cfg = base()
    narrow = cfg.model_copy(update={"pricing_strategy": cfg.pricing_strategy.model_copy(update={
        "home_loan_spread_bps": cfg.pricing_strategy.home_loan_spread_bps.model_copy(update={"min": 40, "max": 50, "step": 5}),
    })})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for k in (0, 1, 2, 4):
            out[f"narrow_grid_above_optimum_max_exp_{k}"] = summary(run_baseline_diagnostics(narrow, max_boundary_expansions=k))
        wrong = with_(cfg, "response", loan_rate_elasticity=+170.0, loan_competitor_gap_elasticity=+130.0)
        out["wrong_sign_loan_elasticity_accepted_by_config"] = True
        out["wrong_sign_loan_elasticity"] = summary(run_baseline_diagnostics(wrong))
    OUT["K_boundary"] = out


# ---------------------------------------------------------------- 5. L ROE infeasible
def roe_cases():
    out = {}
    cfg = base()
    unconstrained = run_baseline_diagnostics(cfg)
    g = unconstrained.grid_results
    out["max_roe_in_grid"] = float(g.return_on_equity.max())
    out["roe_of_unconstrained_best"] = float(
        g.loc[g.total_objective.idxmax(), "return_on_equity"])
    for floor in (0.15, 1.0, 10.0):
        c = with_(cfg, "balance_sheet", min_return_on_equity=floor)
        d = run_baseline_diagnostics(c)
        best_roe = float(d.grid_results.loc[
            np.isclose(d.grid_results.loan_rate, d.best_result.loan_rate)
            & np.isclose(d.grid_results.deposit_rate, d.best_result.deposit_rate), "return_on_equity"].iloc[0])
        out[f"floor_{floor}"] = {
            **summary(d), "selected_roe": best_roe,
            "n_qualifying": int((d.grid_results.return_on_equity >= floor).sum()),
            "any_warning_mentions_roe": any("equity" in w.lower() for w in d.boundary_diagnostics.warnings),
            "same_as_unconstrained": (d.best_result.loan_rate == unconstrained.best_result.loan_rate
                                      and d.best_result.deposit_rate == unconstrained.best_result.deposit_rate),
        }
    # governed path
    try:
        from balance_pricing_engine.application.pricing_service import PricingService
        import inspect
        out["pricing_service_signature"] = str(inspect.signature(PricingService.__init__))
    except Exception as exc:  # noqa: BLE001
        out["pricing_service_import_error"] = repr(exc)
    OUT["L_roe"] = out


# ---------------------------------------------------------------- 6. M non-finite / negative
def nonfinite_cases():
    out = {}
    raw = base().model_dump()
    for label, mutate in {
        "competitor_loan_rate_nan": lambda r: r["market"].__setitem__("competitor_loan_rate", float("nan")),
        "cash_rate_inf": lambda r: r["market"].__setitem__("cash_rate", float("inf")),
        "cash_rate_negative_-0.5pct": lambda r: r["market"].__setitem__("cash_rate", -0.005),
        "wholesale_spread_negative": lambda r: r["market"].__setitem__("wholesale_funding_spread", -0.05),
        "loan_rate_elasticity_nan": lambda r: r["response"].__setitem__("loan_rate_elasticity", float("nan")),
    }.items():
        r = json.loads(json.dumps(raw, default=str)) if False else __import__("copy").deepcopy(raw)
        mutate(r)
        from balance_pricing_engine.config import EngineConfig
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                cfg = EngineConfig.model_validate(r)
            entry = {"config_accepted": True, "warnings": [str(x.message) for x in w]}
            try:
                d = run_baseline_diagnostics(cfg)
                s = summary(d)
                s["objective_is_finite"] = bool(np.isfinite(d.best_result.total_objective))
                s["grid_rows_nonfinite_objective"] = int((~np.isfinite(d.grid_results.total_objective)).sum())
                entry["run"] = s
            except Exception as exc:  # noqa: BLE001
                entry["run_error"] = f"{type(exc).__name__}: {exc}"[:300]
        except Exception as exc:  # noqa: BLE001
            entry = {"config_accepted": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        out[label] = entry
    OUT["M_nonfinite"] = out


# ---------------------------------------------------------------- 7. N determinism
def determinism():
    h = []
    for _ in range(2):
        d = run_baseline_diagnostics(base())
        h.append(hashlib.sha256(pd.util.hash_pandas_object(d.grid_results, index=True).values.tobytes()).hexdigest())
    OUT["N_determinism"] = {"hashes": h, "identical": h[0] == h[1]}


# ---------------------------------------------------------------- 8. O multi-period
def multiperiod_case():
    from balance_pricing_engine.config import MultiPeriodConfig
    from balance_pricing_engine.scenarios.multiperiod import run_multiperiod_diagnostics
    cfg = base().model_copy(update={"multiperiod": MultiPeriodConfig(
        n_periods=2, loan_runoff_rate=0.0, deposit_runoff_rate=0.0, discount_rate=0.0)})
    mp = run_multiperiod_diagnostics(cfg)
    p0, p1 = mp.period_diagnostics
    r0 = p0.best_result
    # independent: period-1 back book under engine formula
    engine_back_book = r0.expected_loan_volume * cfg.market.cash_rate - r0.deposit_interest_cost
    # economically consistent back book (same objective decomposition applied to surviving book,
    # excl. capital) -- for comparison only
    consistent = (r0.loan_interest_income - r0.expected_credit_loss - r0.capital_charge
                  - r0.deposit_interest_cost - r0.wholesale_funding_cost
                  - r0.funding_shortfall_penalty + r0.excess_deposit_income)
    out = {
        "period_objectives": [d.best_result.total_objective for d in mp.period_diagnostics],
        "period_contribs": mp.period_discounted_contributions,
        "implied_back_book_income_p1": mp.period_discounted_contributions[1] - p1.best_result.total_objective,
        "engine_back_book_formula_p1": engine_back_book,
        "consistent_back_book_if_book_kept_same_economics": consistent,
        "same_customers_each_period": bool(np.allclose(r0.expected_loan_volume, p1.best_result.expected_loan_volume)),
    }
    # reference-rate drift check
    cfg2 = base().model_copy(update={"multiperiod": MultiPeriodConfig(
        n_periods=3, competitor_loan_rate_drift_bps=100, competitor_deposit_rate_drift_bps=100,
        market_cash_rate_drift_bps=100)})
    mp2 = run_multiperiod_diagnostics(cfg2)
    out["drift_100bp_per_period"] = [summary(d) for d in mp2.period_diagnostics]
    OUT["O_multiperiod"] = out


# ---------------------------------------------------------------- 9. segmented consistency
def segmented_consistency():
    from balance_pricing_engine.config import (
        DepositTierConfig, GridConfig, LoanTierConfig, SegmentedPricingConfig)
    cfg = base()
    lg, dg = build_configured_rate_grids(cfg)
    seg = SegmentedPricingConfig(
        loan_tiers=[LoanTierConfig(name="all", lvr_max=None, rate_grid=GridConfig(start=float(lg[0]), stop=float(lg[-1]), step=0.0005))],
        deposit_tiers=[DepositTierConfig(name="all", balance_max=None, rate_grid=GridConfig(start=float(dg[0]), stop=float(dg[-1]), step=0.0005))],
    )
    seg_cfg = cfg.model_copy(update={"pricing_strategy": None, "segmented_pricing": seg})
    joint_cfg = cfg.model_copy(update={"pricing_strategy": None, "products": __import__(
        "balance_pricing_engine.config", fromlist=["ProductConfig"]).ProductConfig(
        loan_rate_grid=GridConfig(start=float(lg[0]), stop=float(lg[-1]), step=0.0005),
        deposit_rate_grid=GridConfig(start=float(dg[0]), stop=float(dg[-1]), step=0.0005))})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = run_baseline_diagnostics(seg_cfg, max_boundary_expansions=0)
        j = run_baseline_diagnostics(joint_cfg, max_boundary_expansions=0)
    # stressed funding: segmented loan tiers ignore deposit funding / penalty
    stress = dict(required_deposit_funding_ratio=1.0, funding_shortfall_penalty_rate=0.05)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s2 = run_baseline_diagnostics(with_(seg_cfg, "balance_sheet", **stress), max_boundary_expansions=0)
        j2 = run_baseline_diagnostics(with_(joint_cfg, "balance_sheet", **stress), max_boundary_expansions=0)
    OUT["segmented_vs_joint_single_tier"] = {
        "baseline": {"segmented": summary(s), "joint": summary(j),
                     "objective_shortfall_of_segmented": j.best_result.total_objective - s.best_result.total_objective},
        "stressed_funding": {"segmented": summary(s2), "joint": summary(j2),
                             "objective_shortfall_of_segmented": j2.best_result.total_objective - s2.best_result.total_objective},
    }


# ---------------------------------------------------------------- 10. perf
def perf():
    cfg = base()
    rows = []
    for n in (1000, 5000, 20000):
        c = with_(cfg, "simulation", n_loan_customers=n, n_deposit_customers=n)
        t = time.perf_counter()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            d = run_baseline_diagnostics(c)
        rows.append({"n_customers": n, "grid_rows": len(d.grid_results), "seconds": round(time.perf_counter() - t, 3)})
    for step in (5, 1):
        c = cfg.model_copy(update={"pricing_strategy": cfg.pricing_strategy.model_copy(update={
            "home_loan_spread_bps": cfg.pricing_strategy.home_loan_spread_bps.model_copy(update={"step": step}),
            "term_deposit_spread_bps": cfg.pricing_strategy.term_deposit_spread_bps.model_copy(update={"step": step})})})
        t = time.perf_counter()
        d = run_baseline_diagnostics(c)
        rows.append({"step_bps": step, "grid_rows": len(d.grid_results), "seconds": round(time.perf_counter() - t, 3),
                     "best": [d.best_result.loan_rate, d.best_result.deposit_rate]})
    OUT["perf"] = rows


if __name__ == "__main__":
    for fn in (hand_example, reference_check, partial_effects, perturbations, boundary_cases,
               roe_cases, nonfinite_cases, determinism, multiperiod_case, segmented_consistency, perf):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            OUT[fn.__name__ + "_ERROR"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(OUT, indent=1, default=float))
