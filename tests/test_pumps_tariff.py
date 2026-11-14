"""Pump table, the energy physics check and the electricity tariff."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from tests import synthetic as syn
from wellopt.pumps import build_pump_table, energy_sanity
from wellopt.tariff import Tariff, kw_by_meter


def water_kw(flow_gpm: float, head_ft: float) -> float:
    return flow_gpm * head_ft / 3960 * 0.746  # water horsepower, in kW


def total_head_ft(flow_gpm: float, press_psi: float, static_ft: float = 100.0, sc: float = 20.0) -> float:
    return press_psi * 2.31 + static_ft + flow_gpm / sc


def test_pump_table_learns_flow_power_and_pressure_from_the_data(ctx):
    t = ctx.table
    assert set(t.index) == set(syn.PUMPS)
    for p, (sec, flow, kw, *_rest) in syn.PUMPS.items():
        assert t.at[p, "section"] == sec
        assert t.at[p, "flow_gpm"] == pytest.approx(flow, rel=0.02)
        assert t.at[p, "kw_recorded"] == pytest.approx(kw, rel=0.02)
        assert t.at[p, "press_psi"] == pytest.approx(syn.PRESS_PSI, rel=0.02)
        assert t.at[p, "run_hours"] >= 50
    assert bool(t["available"].all())
    assert not bool(t["must_run"].any())
    assert t["kw_plan"].equals(t["kw_recorded"])  # power_model source = "recorded"


def test_pump_groups(ctx):
    t = ctx.table
    assert pd.isna(t.at["PMP_1", "group"])          # group "0" in the spec sheet means none
    assert t.at["PMP_5", "group"] == t.at["PMP_6", "group"] == "S2"


def test_physics_power_and_implied_efficiency(ctx):
    t = ctx.table
    for p in t.index:
        q, psi = t.at[p, "flow_gpm"], t.at[p, "press_psi"]
        assert t.at[p, "lift_ft"] == pytest.approx(100.0 + q / 20.0)
        assert t.at[p, "kw_physics"] == pytest.approx(water_kw(q, total_head_ft(q, psi)) / 0.70, rel=1e-6)
        # The sanity check uses the least power physics allows: discharge pressure only, no well lift.
        assert t.at[p, "implied_efficiency"] == pytest.approx(water_kw(q, psi * 2.31) / t.at[p, "kw_recorded"],
                                                             rel=1e-6)
    check = energy_sanity(t)
    assert check["pumps_checked"] == 6
    assert check["pumps_above_100pct"] == 0
    assert check["verdict"].startswith("OK")


def test_energy_sanity_fails_when_most_pumps_beat_physics():
    table = pd.DataFrame({"available": [True] * 6, "implied_efficiency": [1.5, 1.4, 1.3, 1.2, 0.5, 0.6]})
    check = energy_sanity(table)
    assert check["pumps_above_100pct"] == 4
    assert check["verdict"].startswith("FAIL")


def test_physics_power_source_plans_with_physics_kw(ctx):
    t = build_pump_table(ctx.qc, ctx.specs, replace(ctx.cfg, power_source="physics"))
    assert t["kw_plan"].equals(t["kw_physics"])


def test_out_of_service_and_must_run(ctx):
    t = build_pump_table(ctx.qc, ctx.specs, replace(ctx.cfg, out_of_service=("PMP_2",), must_run=("PMP_4",)))
    assert not t.at["PMP_2", "available"]
    assert t.at["PMP_4", "must_run"]
    assert int(t["available"].sum()) == 5
    assert int(t["must_run"].sum()) == 1


def test_on_peak_hours_are_weekday_afternoons(ctx):
    tariff = ctx.tariff
    idx = pd.DatetimeIndex(["2019-04-08 13:00", "2019-04-08 14:00", "2019-04-08 19:00", "2019-04-08 20:00",
                            "2019-04-13 15:00"])  # Monday x4, then a Saturday
    assert tariff.is_on_peak(idx).tolist() == [False, True, True, False, False]
    np.testing.assert_allclose(tariff.energy_price(idx), [0.07, 0.14, 0.14, 0.07, 0.07])


def test_bill_across_a_month_boundary(ctx):
    tariff = Tariff(replace(ctx.cfg.tariff, demand_charge_usd_per_kw_month=10.0))
    idx = pd.date_range("2019-03-31 22:00", periods=4, freq="h")  # Sunday night into Monday 1 April
    kw = pd.DataFrame({"M1": [100.0, 50.0, 80.0, 0.0], "M2": [0.0, 0.0, 20.0, 40.0]}, index=idx)
    bill = tariff.cost(kw, {"M1": 120.0, "M2": 10.0})
    assert bill["energy_kwh"] == pytest.approx(290.0)
    assert bill["energy_usd"] == pytest.approx(290.0 * 0.07)
    # March: the earlier peaks (120, 10) stand. April: the new peaks (80, 40).
    assert bill["demand_usd"] == pytest.approx((120 + 10 + 80 + 40) * 10.0)
    assert bill["total_usd"] == pytest.approx(bill["energy_usd"] + bill["demand_usd"])


def test_kw_by_meter_adds_pumps_on_a_shared_meter():
    table = pd.DataFrame({"meter": ["N12", "N12", "PMP_3"], "kw_plan": [10.0, 20.0, 5.0]},
                         index=["PMP_1", "PMP_2", "PMP_3"])
    on = pd.DataFrame({"PMP_1": [1.0, 0.0], "PMP_2": [1.0, 1.0], "PMP_3": [0.0, 1.0]},
                      index=pd.date_range("2019-01-01", periods=2, freq="h"))
    out = kw_by_meter(on, table)
    assert out["N12"].tolist() == [30.0, 20.0]
    assert out["PMP_3"].tolist() == [0.0, 5.0]
