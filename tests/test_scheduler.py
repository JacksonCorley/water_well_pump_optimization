"""Section plans: building, notes, serialization and what-if re-planning (wellopt.scheduler)."""
from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from tests import synthetic as syn
from wellopt.scheduler import SectionPlan
from wellopt.scheduler import replan as replan_plan

NORTH = ["PMP_1", "PMP_2", "PMP_3"]


def test_plan_shape_and_summary(north_plan):
    plan = north_plan
    assert plan.section == "North"
    assert plan.status in ("optimal", "staged", "time_limit")
    assert plan.schedule.shape == (24, 3)
    assert sorted(plan.schedule.columns) == NORTH
    assert plan.schedule.index[0] == syn.PLAN_AT
    assert set(np.unique(plan.schedule.to_numpy())) <= {0, 1}
    s = plan.summary
    assert s["storage_now_mg"] == pytest.approx(0.36)      # 60% of the 0.6 MG assumed tank
    assert s["storage_measured"] is False
    assert s["forecaster"] == "naive"
    assert s["hours"] == 24
    assert s["total_usd"] == pytest.approx(s["energy_usd"] + s["start_usd"] + s["demand_usd"] + s["penalty_usd"])


def test_plan_notes_say_storage_is_assumed(north_plan):
    assumed = [n for n in north_plan.notes if n.startswith("Storage is ASSUMED")]
    assert len(assumed) == 1
    assert "configured starting level (60% of 0.6 MG)" in assumed[0]


def test_timeline_is_consistent_with_the_schedule(north_plan):
    plan, tl = north_plan, north_plan.timeline
    flows = plan.pump_table().loc[plan.schedule.columns, "flow_gpm"].astype(float)
    np.testing.assert_allclose(tl["production_gpm"], plan.schedule.to_numpy() @ flows.to_numpy())
    np.testing.assert_allclose(tl["pumps_on"], plan.schedule.sum(axis=1))
    expected = 0.36 + np.cumsum((tl["production_gpm"] - tl["demand_p50"]) * 60 / 1e6)
    np.testing.assert_allclose(tl["storage_p50_mg"], expected)
    assert (tl["storage_high_demand_mg"] <= tl["storage_p50_mg"] + 1e-9).all()
    assert (tl["storage_low_demand_mg"] >= tl["storage_p50_mg"] - 1e-9).all()
    np.testing.assert_allclose(tl["energy_usd"], tl["kw"] * tl["price_usd_per_kwh"])


def test_plan_survives_a_json_round_trip(north_plan):
    back = SectionPlan.from_record(json.loads(json.dumps(north_plan.to_record())))
    pd.testing.assert_frame_equal(back.schedule, north_plan.schedule, check_freq=False, check_dtype=False)
    pd.testing.assert_frame_equal(back.timeline, north_plan.timeline, check_freq=False, check_dtype=False)
    assert back.notes == north_plan.notes
    assert back.summary["total_usd"] == pytest.approx(north_plan.summary["total_usd"])
    assert sorted(back.pump_table().index) == NORTH


def test_replan_without_changes_reproduces_the_plan(ctx, north_plan):
    again = replan_plan(north_plan, ctx.cfg, ctx.tariff)
    assert again.summary["storage_now_mg"] == pytest.approx(0.36)
    if north_plan.status != "time_limit":  # a time-limited solve may stop at a different incumbent
        pd.testing.assert_frame_equal(again.schedule, north_plan.schedule, check_freq=False, check_dtype=False)
        assert again.summary["total_usd"] == pytest.approx(north_plan.summary["total_usd"], rel=1e-9)


def test_what_if_storage_and_pump_out_of_service(ctx, north_plan):
    busiest = north_plan.schedule.sum().idxmax()
    what_if = replan_plan(north_plan, ctx.cfg, ctx.tariff, storage_mg=0.5, out_of_service=[busiest])
    assert what_if.summary["storage_now_mg"] == pytest.approx(0.5)
    assert busiest not in what_if.schedule.columns  # left out of the plan; the dashboard shows it as out of service
    assert busiest in what_if.inputs["state"]["unavailable"]
    tl = what_if.timeline
    assert tl["storage_p50_mg"].iloc[0] == pytest.approx(0.5 + (tl["production_gpm"].iloc[0]
                                                                - tl["demand_p50"].iloc[0]) * 60 / 1e6)
    assert any(busiest in n for n in what_if.notes)


def test_replan_with_the_rule(ctx, north_plan):
    plan = replan_plan(north_plan, ctx.cfg, ctx.tariff, method="rule")
    assert plan.status == "rule"
    assert any(n.startswith("Rule-based plan") for n in plan.notes)


def test_replan_needs_the_stored_inputs(ctx, north_plan):
    with pytest.raises(ValueError, match="without its inputs"):
        replan_plan(replace(north_plan, inputs={}), ctx.cfg, ctx.tariff)
