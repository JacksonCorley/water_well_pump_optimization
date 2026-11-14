"""The scheduling MILP, its rule-based fallback and the shared cost function (wellopt.milp)."""
from __future__ import annotations

import itertools

import numpy as np
import pytest

from wellopt import milp
from wellopt.milp import ScheduleProblem, evaluate_schedule, initial_locks, rule_plan, solve


def tiny_problem(**overrides) -> ScheduleProblem:
    """Two pumps, six hours, prices that peak in hours 2-3, and demand charges above a prior peak."""
    base = dict(
        pumps=["A", "B"], flow_gpm=np.array([1000.0, 600.0]), kw=np.array([80.0, 30.0]),
        price=np.array([0.07, 0.07, 0.14, 0.14, 0.07, 0.07]),
        demand_p50=np.full(6, 900.0), demand_high=np.full(6, 1000.0), demand_low=np.full(6, 850.0),
        storage0_mg=0.30, min_mg=0.20, floor_mg=0.10, max_mg=0.40, end_mg=0.28,
        on0=np.array([1, 0]), run_hours0=np.array([1, 5]),
        min_up=2, min_down=2, max_starts_per_day=2,
        demand_rate=12.0, meters=["A", "B"], prior_peak_kw={("A", 0): 50.0, ("B", 0): 0.0},
        risk_horizon=3, mip_gap=1e-9, stage_hours=None,
    )
    base.update(overrides)
    return ScheduleProblem(**base)


def feasible(pr: ScheduleProblem, on: np.ndarray, check_starts: bool = True) -> bool:
    """Operating rules, written independently of the MILP: minimum up/down times
    (including time already spent before the plan), the start limit in each 24-hour
    block of the plan, must-run and out-of-service pumps, and group flow limits."""
    on = np.asarray(on, int)
    must, off = pr.masks()
    for p in range(pr.P):
        if off[p] or must[p]:  # out of service / must run: fixed for the whole plan, run times aside
            if on[p].any() if off[p] else not on[p].all():
                return False
            continue
        prev, run, starts = int(pr.on0[p]), int(pr.run_hours0[p]), np.zeros(pr.H, int)
        for t in range(pr.H):
            u = int(on[p, t])
            if u != prev:
                if (prev == 1 and run < pr.min_up) or (prev == 0 and run < pr.min_down):
                    return False
                starts[t] = u
                run = 1
            else:
                run += 1
            prev = u
        if check_starts and any(starts[a:a + 24].sum() > pr.max_starts_per_day for a in range(0, pr.H, 24)):
            return False
    for members, cap in pr.group_limits:
        if np.any((np.asarray(members, float) * pr.flow_gpm) @ on > cap + 1e-6):
            return False
    return True


def brute_force(pr: ScheduleProblem) -> tuple[float, np.ndarray]:
    best = (np.inf, None)
    for bits in itertools.product((0, 1), repeat=pr.P * pr.H):
        on = np.array(bits).reshape(pr.P, pr.H)
        if feasible(pr, on):
            total = evaluate_schedule(pr, on)["cost"]["total_usd"]
            if total < best[0]:
                best = (total, on)
    return best


def three_pump_problem(hours: int = 48, **overrides) -> ScheduleProblem:
    t = np.arange(hours)
    demand = 1400 + 500 * np.sin(2 * np.pi * (t - 9) / 24)
    base = dict(
        pumps=["P1", "P2", "P3"], flow_gpm=np.array([1000.0, 800.0, 600.0]), kw=np.array([70.0, 64.0, 54.0]),
        price=np.where((t % 24 >= 14) & (t % 24 < 20), 0.14, 0.07),
        demand_p50=demand, demand_high=demand * 1.1, demand_low=demand * 0.9,
        storage0_mg=0.36, min_mg=0.18, floor_mg=0.06, max_mg=0.57, end_mg=0.33,
        on0=np.array([1, 1, 0]), run_hours0=np.array([1, 8, 3]),
        min_up=3, min_down=2, max_starts_per_day=2, time_limit_s=10.0, stage_hours=6,  # as in production
    )
    base.update(overrides)
    return ScheduleProblem(**base)


def test_milp_finds_the_brute_force_optimum():
    pr = tiny_problem()
    best_total, _ = brute_force(pr)
    res = solve(pr)
    assert res.status == "optimal"
    assert feasible(pr, res.on)
    assert res.cost["total_usd"] == pytest.approx(best_total, abs=1e-4)
    # The MILP's reported cost is the shared cost function applied to its schedule.
    assert res.cost == pytest.approx(evaluate_schedule(pr, res.on)["cost"])


def test_relax_and_fix_stages_stay_feasible_and_never_beat_the_optimum():
    pr = tiny_problem(stage_hours=3)
    best_total, _ = brute_force(pr)
    res = solve(pr)
    assert res.status == "staged"
    assert feasible(pr, res.on)
    assert res.cost["total_usd"] >= best_total - 1e-6


def test_longer_problem_obeys_the_operating_rules():
    pr = three_pump_problem()
    res = solve(pr)
    assert res.status in ("staged", "time_limit")
    assert res.on.shape == (3, 48)
    assert feasible(pr, res.on)
    assert res.on[0, :2].all()  # P1 has run 1 of its 3 minimum hours
    assert res.cost["penalty_usd"] < 1.0


def test_must_run_and_out_of_service():
    pr = three_pump_problem(must_run=np.array([False, False, True]), force_off=np.array([True, False, False]))
    res = solve(pr)
    assert feasible(pr, res.on)
    assert res.on[2].all()
    assert not res.on[0].any()  # out of service wins over P1's remaining minimum run time


def test_initial_locks():
    pr = tiny_problem(on0=np.array([1, 0]), run_hours0=np.array([1, 1]), min_up=3, min_down=3)
    lo, hi = initial_locks(pr)
    assert lo.tolist() == [[1, 1, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0]]
    assert hi.tolist() == [[1] * 6, [0, 0, 1, 1, 1, 1]]
    lo, hi = initial_locks(tiny_problem(force_off=np.array([True, False])))
    assert lo[0].tolist() == [0] * 6 and hi[0].tolist() == [0] * 6


def test_group_limit_caps_combined_flow():
    pr = tiny_problem(demand_p50=np.full(6, 1500.0), demand_high=np.full(6, 1600.0), demand_low=np.full(6, 1400.0),
                      group_limits=[(np.array([True, True]), 1200.0)])
    res = solve(pr)
    assert feasible(pr, res.on)
    assert (pr.flow_gpm @ res.on <= 1200.0 + 1e-6).all()
    assert res.cost["penalty_usd"] > 0  # demand above the cap must drain storage


def test_demand_above_capacity_runs_everything_and_reports_the_shortfall():
    pr = tiny_problem(demand_p50=np.full(6, 3000.0), demand_high=np.full(6, 3200.0), demand_low=np.full(6, 2800.0))
    res = solve(pr)
    assert res.on.all()
    assert res.cost["penalty_usd"] > 0


@pytest.mark.parametrize("behaviour, message", [("raise", "solver crashed"), ("none", "no solution")])
def test_solver_failure_falls_back_to_the_rule(monkeypatch, behaviour, message):
    def broken(_pr):
        if behaviour == "raise":
            raise RuntimeError("solver crashed")
        return None
    monkeypatch.setattr(milp, "solve_milp", broken)
    pr = three_pump_problem()
    res = solve(pr)
    assert res.status == "fallback"
    assert message in res.message
    assert feasible(pr, res.on, check_starts=False)


def test_rule_method_and_rule_plan():
    pr = three_pump_problem()
    res = solve(pr, method="rule")
    assert res.status == "rule"
    on = rule_plan(pr)
    assert on.shape == (3, 48)
    assert feasible(pr, on, check_starts=False)  # the rule respects run times, not the start limit
    np.testing.assert_array_equal(res.on, on)


def test_band_shifts_widen_for_the_risk_horizon_then_stop():
    hi, lo = tiny_problem().band_shifts()
    np.testing.assert_allclose(hi, [0.006, 0.012, 0.018, 0.018, 0.018, 0.018])
    np.testing.assert_allclose(lo, [0.003, 0.006, 0.009, 0.009, 0.009, 0.009])


def test_evaluate_schedule_arithmetic():
    pr = tiny_problem()
    on = np.array([[1, 1, 1, 0, 0, 0], [0, 0, 1, 1, 1, 1]])
    out = evaluate_schedule(pr, on)
    assert out["starts"] == 1                                   # A was already running
    c = out["cost"]
    assert c["start_usd"] == pytest.approx(10.0)
    assert c["energy_usd"] == pytest.approx(80 * (0.07 + 0.07 + 0.14) + 30 * (0.14 + 0.14 + 0.07 + 0.07))
    assert c["demand_usd"] == pytest.approx((80 - 50) * 12 + 30 * 12)   # only the rise above the prior peak
    prod = np.array([1000, 1000, 1600, 600, 600, 600])
    expected = 0.30 + np.concatenate([[0], np.cumsum((prod - 900) * 60 / 1e6)])
    np.testing.assert_allclose(out["storage"]["p50"], expected)
    assert c["total_usd"] == pytest.approx(c["energy_usd"] + c["start_usd"] + c["demand_usd"] + c["penalty_usd"])
