"""Closed-loop policy backtest and its report (wellopt.backtest, wellopt.reporting)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests import synthetic as syn
from tests.conftest import SMALL_BACKTEST_POLICIES
from wellopt.__main__ import backtest_window
from wellopt.backtest import legacy_hourly_schedule, simulate, tank_step
from wellopt.dashboard import read_backtest
from wellopt.milp import MG_PER_GPM_HOUR
from wellopt.reporting import key_findings, policy_label, write_backtest

STORAGE_AWARE = ["rule_naive", "milp_naive", "milp_perfect"]


@pytest.mark.parametrize("level, net, expected", [
    (0.30, 0.10, (0.40, 0.0, 0.0)),
    (0.55, 0.10, (0.60, 0.05, 0.0)),   # full: the rest spills
    (0.05, -0.10, (0.0, 0.0, 0.05)),   # empty: the rest is unserved
])
def test_tank_step(level, net, expected):
    assert tank_step(level, net, 0.6) == pytest.approx(expected)


def test_legacy_method_picks_the_cheapest_set_each_hour():
    flow, kw = np.array([1000.0, 600.0, 500.0]), np.array([90.0, 40.0, 35.0])
    demand = np.array([0, 450, 550, 1050, 1200, 5000])
    on = legacy_hourly_schedule(demand, flow, kw)
    assert on.shape == (3, 6)
    assert on.T.tolist() == [[0, 0, 0], [0, 0, 1], [0, 1, 0], [0, 1, 1], [1, 0, 1], [1, 1, 1]]
    forced = legacy_hourly_schedule(np.array([450]), flow, kw, must=np.array([True, False, False]))
    assert forced.T.tolist() == [[1, 0, 0]]


DATA_END = pd.Timestamp("2019-04-10 23:00")


@pytest.mark.parametrize("kwargs, expected", [
    ({}, ("2019-03-01", "2019-04-01")),                                 # last whole billing month
    ({"start": "2019-04-05", "days": 2}, ("2019-04-05", "2019-04-07")),
    ({"start": "2019-04-05", "days": 30}, ("2019-04-05", "2019-04-09")),  # clipped to leave a full horizon
    ({"days": 7}, ("2019-04-02", "2019-04-09")),
    ({"start": "2019-04-01", "end": "2019-04-03"}, ("2019-04-01", "2019-04-03")),
])
def test_backtest_window(kwargs, expected):
    start, end = backtest_window(DATA_END, 24, **kwargs)
    assert (start, end) == (pd.Timestamp(expected[0]), pd.Timestamp(expected[1]))


def test_small_backtest_table(ctx, small_backtest):
    table, results = small_backtest
    assert list(table["policy"]) == SMALL_BACKTEST_POLICIES
    assert set(results) == {("North", p) for p in SMALL_BACKTEST_POLICIES}
    t = table.set_index("policy")
    assert t.loc["historical", "savings_vs_historical_pct"] == pytest.approx(0.0)
    # The original method falls short only in hours when demand is above what every pump together delivers.
    legacy = results[("North", "legacy_hourly")]
    over = (legacy.hourly["demand_gpm"] > ctx.table.loc[legacy.on.columns, "flow_gpm"].sum() + 1e-6).to_numpy()
    assert t.loc["legacy_hourly", "unmet_demand_hours"] == over.sum()
    assert legacy.on.to_numpy()[over].all()
    for p in STORAGE_AWARE:
        assert t.loc[p, "replans"] == 8          # 48 h, re-planned every 6 h
        assert t.loc[p, "fallbacks"] == 0
        assert t.loc[p, "unserved_mg"] == pytest.approx(0.0)
    # Water balance over the window: pumped = demand + storage gained + spilled - unserved.
    start_mg = ctx.cfg.storage["North"].initial_mg
    for p in STORAGE_AWARE:
        r = t.loc[p]
        end_mg = results[("North", p)].hourly["storage_mg"].iloc[-1]
        assert r["pumped_mg"] == pytest.approx(r["demand_mg"] + (end_mg - start_mg) + r["spilled_mg"]
                                               - r["unserved_mg"], abs=1e-9)


def test_simulated_tank_is_physical_and_balances(ctx, small_backtest):
    _, results = small_backtest
    usable = ctx.cfg.storage["North"].usable_mg
    for p in STORAGE_AWARE:
        h = results[("North", p)].hourly
        assert len(h) == 48
        assert h["storage_mg"].between(0.0, usable + 1e-9).all()
        assert (h["spilled_mg"] >= 0).all() and (h["unserved_mg"] >= 0).all()
        change = h["storage_mg"].diff().iloc[1:]
        net = ((h["production_gpm"] - h["demand_gpm"]) * MG_PER_GPM_HOUR - h["spilled_mg"] + h["unserved_mg"]).iloc[1:]
        np.testing.assert_allclose(change, net, atol=1e-9)


def test_unknown_policy_is_rejected(ctx):
    with pytest.raises(ValueError, match="unknown policy"):
        simulate("North", "milp_magic", syn.BT_START, syn.BT_END, qc=ctx.qc, table=ctx.table, cfg=ctx.cfg,
                 tariff=ctx.tariff)


def test_backtest_report_files(ctx, small_backtest, tmp_path):
    table, results = small_backtest
    c = syn.ctx_in(ctx, tmp_path)
    md = write_backtest(table, results, c, syn.BT_START, syn.BT_END, 6)
    assert md == tmp_path / "reports" / "backtest_summary.md"
    text = md.read_text(encoding="utf-8")
    assert "## Key findings" in text and "## North" in text
    loaded = read_backtest(c.reports_dir())
    assert loaded is not None
    assert list(loaded["summary"]["policy"]) == SMALL_BACKTEST_POLICIES
    assert loaded["meta"]["sections"] == ["North"]
    assert set(loaded["hourly"]["policy"]) == set(SMALL_BACKTEST_POLICIES)
    assert len(loaded["hourly"]) == 48 * len(SMALL_BACKTEST_POLICIES)
    assert (tmp_path / "reports" / "backtest_on.csv.gz").exists()


def _row(policy, total, saving, **kw):
    row = dict(section="North", policy=policy, total_usd_storage_adjusted=total, savings_vs_historical_pct=saving,
               energy_savings_vs_historical_pct=saving, demand_usd=100.0, kwh_savings_pct=1.0,
               kwh_savings_physics_pct=-1.0, starts=10, starts_per_pump_day_max=2, storage_min_mg=0.30,
               hours_below_min=0, spilled_mg=0.0, hours_spilling=0, unserved_mg=0.0, hours_unserved=0)
    row.update(kw)
    return row


META = {"sections": ["North"], "storage": {"North": {"min_mg": 0.18, "floor_mg": 0.06, "usable_mg": 0.6}}}


def test_key_findings_skip_policies_that_leave_demand_unserved():
    table = pd.DataFrame([_row("historical", 1000.0, 0.0),
                          _row("milp_gbm", 800.0, 20.0, unserved_mg=0.5, hours_unserved=3),
                          _row("rule_gbm", 900.0, 10.0)])
    text = "\n".join(key_findings(table, META))
    assert "left 0.50 MG of demand unserved" in text
    assert f"the best runnable policy, {policy_label('rule_gbm')}, cuts the bill by 10.0%" in text
    assert "energy cost 10.0% lower" in text
    assert "falls 1.0% with recorded kW and rises 1.0% with the physics power model" in text
    assert "stayed above the 0.18 MG reserve" in text


def test_key_findings_word_a_negative_saving_as_an_increase():
    table = pd.DataFrame([_row("historical", 1000.0, 0.0),
                          _row("milp_gbm", 1025.0, -2.5, hours_below_min=4, storage_min_mg=0.05)])
    text = "\n".join(key_findings(table, META))
    assert "raises the bill by 2.5%" in text
    assert "energy cost 2.5% higher" in text
    assert "-2.5" not in text
    assert "under the 0.18 MG reserve for 4 h, bottoming at 0.05 MG (below the 0.06 MG floor" in text
