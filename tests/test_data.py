"""Loading and quality control of the hourly export (wellopt.data)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests import synthetic as syn
from wellopt.data import fill_for_model, load_history, reconstruct_demand, run_lengths


def test_qc_summary_counts_the_planted_problems(ctx):
    s = ctx.qc.summary
    assert s["hours"] == syn.DAYS * 24
    assert s["missing_rows"] == 1
    assert s["dst_spring_gaps"] == [str(syn.DST_GAP + syn.H1)]
    assert s["long_gaps"] == []
    north, south = s["sections"]["North"], s["sections"]["South"]
    assert (north["invalid_pump_reading"], north["above_capacity"], north["all_pumps_zero"]) == (1, 1, 0)
    assert (south["invalid_pump_reading"], south["above_capacity"], south["all_pumps_zero"]) == (0, 0, 1)
    assert north["valid_hours"] == syn.DAYS * 24 - 3
    assert south["valid_hours"] == syn.DAYS * 24 - 2


def test_qc_flags_name_each_bad_hour(ctx):
    flags = ctx.qc.flags
    north = flags["North"][flags["North"] != ""].to_dict()
    south = flags["South"][flags["South"] != ""].to_dict()
    assert north == {syn.DST_GAP: "missing_row", syn.NEG_FLOW: "invalid_pump_reading",
                     syn.ABOVE_CAP: "above_capacity"}
    assert south == {syn.DST_GAP: "missing_row", syn.ALL_ZERO: "all_pumps_zero"}


def test_qc_counts_rejected_readings_per_pump(ctx):
    issues = ctx.qc.pump_issues
    assert issues.loc["PMP_2", "bad_flow"] == 1        # -50 gpm
    assert issues.loc["PMP_5", "bad_press"] == 1       # running at -5 psi
    assert issues.loc["PMP_6", "flow_without_kw"] == 1
    assert issues["bad_kw"].sum() == 0
    assert int(issues.to_numpy().sum()) == 3
    # A bad pressure reading removes the pressure only; the flow (and so production) stays.
    assert np.isnan(ctx.qc.press.at[syn.BAD_PRESS, "PMP_5"])
    assert ctx.qc.flow.at[syn.BAD_PRESS, "PMP_5"] == 1300.0
    assert np.isfinite(ctx.qc.production.at[syn.BAD_PRESS, "South"])


def test_rejected_hours_are_nan_in_production_and_filled_for_models(ctx):
    qc = ctx.qc
    for t, sec in [(syn.NEG_FLOW, "North"), (syn.ABOVE_CAP, "North"), (syn.ALL_ZERO, "South"),
                   (syn.DST_GAP, "North"), (syn.DST_GAP, "South")]:
        assert np.isnan(qc.production.at[t, sec])
    assert not qc.production_filled.isna().any().any()
    pd.testing.assert_frame_equal(qc.imputed, qc.production.isna(), check_names=False)
    # An isolated missing hour is the mean of its neighbours.
    y, t = qc.production["North"], syn.NEG_FLOW
    assert qc.production_filled.at[t, "North"] == pytest.approx((y[t - syn.H1] + y[t + syn.H1]) / 2)


def test_fill_for_model_interpolates_short_gaps_and_uses_last_week_for_long_ones():
    rng = np.random.default_rng(0)
    idx = pd.date_range("2019-01-01", periods=24 * 21, freq="h")
    y = pd.Series(1000 + 100 * rng.standard_normal(len(idx)), index=idx)
    y.iloc[200:202] = np.nan   # 2 h: interpolated
    y.iloc[400:410] = np.nan   # 10 h: every hour from the same hour a week earlier
    filled, imputed = fill_for_model(y, short_gap_hours=3)
    assert int(imputed.sum()) == 12
    assert not filled.isna().any()
    expected_short = np.interp([200, 201], [199, 202], [y.iloc[199], y.iloc[202]])
    np.testing.assert_allclose(filled.iloc[200:202], expected_short)
    np.testing.assert_allclose(filled.iloc[400:410], y.iloc[400 - 168:410 - 168])


def test_fill_for_model_edges():
    idx = pd.date_range("2019-01-01", periods=6, freq="h")
    y = pd.Series([np.nan, 10.0, 20.0, 30.0, np.nan, np.nan], index=idx)
    filled, imputed = fill_for_model(y)
    assert filled.tolist() == [10.0, 10.0, 20.0, 30.0, 30.0, 30.0]  # no lags this early: nearest value
    assert imputed.tolist() == [True, False, False, False, True, True]


def test_reconstruct_demand_adds_tank_drawdown():
    idx = pd.date_range("2019-01-01", periods=4, freq="h")
    production = pd.Series(1000.0, index=idx)
    tank = pd.Series([0.50, 0.56, 0.56, 0.50], index=idx)  # +0.06 MG in an hour = 1000 gpm into storage
    demand = reconstruct_demand(production, tank)
    np.testing.assert_allclose(demand.to_numpy(), [np.nan, 0.0, 1000.0, 2000.0], atol=1e-6)
    assert reconstruct_demand(production, None) is production
    assert reconstruct_demand(production, pd.Series(np.nan, index=idx)) is production


def test_run_lengths_count_hours_in_the_current_state():
    on = pd.Series([1, 1, 0, 0, 0, 1, np.nan, np.nan], dtype=float)
    assert run_lengths(on).tolist() == [1, 2, 1, 2, 3, 1, 1, 2]


def test_load_history_sorts_and_keeps_the_first_of_repeated_hours(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text("Time,A\n2019-01-01 02:00,3\n2019-01-01 00:00,1\n"
                    "2019-01-01 01:00,2\n2019-01-01 01:00,99\n", encoding="utf-8")
    raw = load_history(path)
    assert raw.index.name == "time"
    assert list(raw.index) == list(pd.date_range("2019-01-01", periods=3, freq="h"))
    assert raw["A"].tolist() == [1, 2, 3]
