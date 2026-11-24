"""Per-pump performance models and the energy-data sanity check.

The scheduler needs, for each pump, the flow it delivers and the power it draws
when running. Two sources are supported:

* recorded: mean flow and kW over valid running hours in the SCADA data;
* physics:  kW = flow x (discharge head + well lift) / (3960 x efficiency),
            with lift = static level + flow / specific capacity.

The sanity check compares recorded kW with the *minimum* power physics allows
(discharge pressure only, no lift, 100% efficiency). Recorded values below that
floor cannot be right as labeled.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import SiteConfig
from .data import QCResult

FT_PER_PSI = 2.31
HP_PER_KW = 1 / 0.746
WHP_DIVISOR = 3960.0  # gpm * ft / 3960 = water horsepower


def total_dynamic_head(flow_gpm, press_psi, static_ft: float, specific_capacity_gpm_ft: float):
    """Estimated total dynamic head (ft) a well pump works against: static water level, plus drawdown
    (flow / specific capacity), plus discharge pressure. Column friction is ignored, and head is not
    measured anywhere, so the result is only as good as the spec sheet's static level and specific capacity.

    Works on scalars, numpy arrays and pandas Series; missing flow or pressure gives NaN."""
    sc = float(specific_capacity_gpm_ft) if pd.notna(specific_capacity_gpm_ft) else 0.0
    drawdown = flow_gpm / sc if sc > 0 else flow_gpm * 0.0
    return float(static_ft) + drawdown + press_psi * FT_PER_PSI


def _r2(y: np.ndarray, yhat: np.ndarray) -> float:
    ss = ((y - y.mean()) ** 2).sum()
    return float(1 - ((y - yhat) ** 2).sum() / ss) if ss > 0 else float("nan")


def build_pump_table(qc: QCResult, specs: pd.DataFrame, cfg: SiteConfig) -> pd.DataFrame:
    """One row per pump with everything the scheduler and the reports need."""
    eff = cfg.wire_to_water_efficiency
    rows = []
    for pump, spec in specs.iterrows():
        has_data = pump in qc.flow.columns
        row = {
            "pump": pump, "section": spec["section"], "group": spec["group"], "meter": spec["meter"],
            "x": spec["x"], "y": spec["y"],
            "spec_flow_gpm": spec["spec_flow_gpm"], "spec_kw": spec["spec_kw"],
            "static_ft": spec["static_ft"], "specific_capacity_gpm_ft": spec["specific_capacity_gpm_ft"],
        }
        flow_q = spec["spec_flow_gpm"]
        kw = spec["spec_kw"]
        press = spec["spec_press_psi"]
        run_hours, duty, r2_q, r2_qp = 0, 0.0, np.nan, np.nan
        if has_data:
            running = (qc.on[pump] == 1) & qc.flow[pump].notna() & qc.power[pump].gt(0) & qc.press[pump].notna()
            run_hours = int(running.sum())
            known = qc.on[pump].notna()
            duty = float(running.sum() / known.sum()) if known.any() else 0.0
            if run_hours >= 50:
                f = qc.flow.loc[running, pump].to_numpy()
                w = qc.power.loc[running, pump].to_numpy()
                pr = qc.press.loc[running, pump].to_numpy()
                flow_q, kw, press = float(f.mean()), float(w.mean()), float(pr.mean())
                a = (f @ w) / (f @ f)
                r2_q = _r2(w, a * f)
                X = np.c_[f, f * pr]
                coef, *_ = np.linalg.lstsq(X, w, rcond=None)
                r2_qp = _r2(w, X @ coef)
        sc = spec["specific_capacity_gpm_ft"]
        lift = spec["static_ft"] + (flow_q / sc if sc and sc > 0 else 0.0)
        whp_min = flow_q * press * FT_PER_PSI / WHP_DIVISOR
        kw_physics = flow_q * (press * FT_PER_PSI + lift) / WHP_DIVISOR / HP_PER_KW / eff if flow_q > 0 else 0.0
        row.update({
            "flow_gpm": flow_q, "kw_recorded": kw, "kw_physics": kw_physics, "press_psi": press,
            "lift_ft": lift, "run_hours": run_hours, "duty": duty,
            "implied_efficiency": (whp_min / (kw * HP_PER_KW)) if kw > 0 else np.nan,
            "r2_kw_vs_flow": r2_q, "r2_kw_vs_flow_and_pressure": r2_qp,
            "available": bool(has_data and flow_q > 0 and kw > 0 and pump not in cfg.out_of_service),
            "must_run": pump in cfg.must_run,
        })
        rows.append(row)
    table = pd.DataFrame(rows).set_index("pump")
    source = "kw_physics" if cfg.power_source == "physics" else "kw_recorded"
    table["kw_plan"] = table[source]
    table["kwh_per_kgal"] = table["kw_plan"] / (table["flow_gpm"] * 0.06)
    table.loc[table["flow_gpm"] <= 0, "kwh_per_kgal"] = np.nan
    return table


def energy_sanity(table: pd.DataFrame) -> dict:
    """Summarize how many pumps record less power than physics allows."""
    t = table[table["available"]]
    eff = t["implied_efficiency"]
    return {
        "pumps_checked": int(len(t)),
        "pumps_above_100pct": int((eff > 1.0).sum()),
        "median_implied_efficiency": float(eff.median()),
        "verdict": ("FAIL: recorded kW is below the physical minimum for most pumps; "
                    "check units and aggregation against utility bills before quoting kWh or $.")
        if (eff > 1.0).mean() > 0.5 else "OK: recorded kW is physically plausible.",
    }


def section_pumps(table: pd.DataFrame, section: str) -> pd.DataFrame:
    return table[(table["section"] == section)]


def hourly_energy_kwh(on: pd.DataFrame, table: pd.DataFrame) -> pd.Series:
    """kWh per hour for an on/off schedule, priced with the planning power model."""
    kw = table.loc[on.columns, "kw_plan"].to_numpy()
    return pd.Series(on.fillna(0).to_numpy() @ kw, index=on.index)
