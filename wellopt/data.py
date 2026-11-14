"""Load the hourly SCADA export and run rule-based quality control.

Conventions used everywhere downstream:
* timestamps are naive local wall-clock hours on a regular hourly grid
  (the live adapters convert historian UTC to this before QC);
* per-pump flow: 0 means the pump is off, NaN means the reading is unknown;
* section production is the sum of its pumps' flow in gpm. It is NaN when any
  pump reading is unknown or the hour fails a check, and every reason is recorded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .config import SECTIONS


@dataclass
class QCResult:
    flow: pd.DataFrame               # gpm per pump (0 = off, NaN = unknown)
    power: pd.DataFrame              # kW per pump
    press: pd.DataFrame              # psi per pump
    on: pd.DataFrame                 # 1.0 running, 0.0 off, NaN unknown
    production: pd.DataFrame         # section gpm, NaN where invalid
    production_filled: pd.DataFrame  # gaps filled, for model inputs only
    imputed: pd.DataFrame            # True where production_filled is imputed
    flags: pd.DataFrame              # per section: "" when clean, else comma-joined reasons
    pump_issues: pd.DataFrame        # per pump counts of rejected readings by rule
    summary: dict = field(default_factory=dict)
    tank_mg: pd.DataFrame | None = None  # optional tank levels per section (MG)

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.production.index


def load_history(path: str | Path) -> pd.DataFrame:
    """Read a SCADA export with one timestamp column first and one row per hour."""
    raw = pd.read_csv(path)
    ts_col = raw.columns[0]
    raw[ts_col] = pd.to_datetime(raw[ts_col])
    raw = raw.set_index(ts_col).sort_index(kind="stable")  # stable, so a repeated hour keeps its first row
    raw = raw[~raw.index.duplicated(keep="first")]
    raw.index.name = "time"
    return raw


def _append_flag(flags: pd.Series, mask: pd.Series, label: str) -> pd.Series:
    mask = mask.reindex(flags.index, fill_value=False).astype(bool)
    add = np.where(flags[mask] == "", label, flags[mask] + "," + label)
    out = flags.copy()
    out[mask] = add
    return out


def fill_for_model(y: pd.Series, short_gap_hours: int = 3) -> tuple[pd.Series, pd.Series]:
    """Fill gaps for model inputs and return (filled, imputed_mask).

    Gaps of up to `short_gap_hours` are interpolated; longer gaps take the value
    from the same hour last week, then yesterday, then the nearest observation.
    """
    missing = y.isna()
    run_id = missing.ne(missing.shift()).cumsum()
    gap_len = missing.groupby(run_id).transform("sum")
    # Interpolate whole short gaps only. (interpolate(limit=n) would also fill the
    # first n hours of every long gap, ramping toward a value a day or more away.)
    short = missing & (gap_len <= short_gap_hours)
    filled = y.where(~short, y.interpolate(limit_area="inside"))
    for lag in (168, 24, 336):
        filled = filled.fillna(filled.shift(lag))
    filled = filled.ffill().bfill()
    return filled, missing & filled.notna()


def quality_check(
    raw: pd.DataFrame,
    specs: pd.DataFrame,
    *,
    on_threshold_frac: float = 0.05,
    max_flow_mult: float = 2.0,
    max_kw_mult: float = 4.0,
    capacity_mult: float = 1.2,
    short_gap_hours: int = 3,
    tank_columns: dict[str, str] | None = None,
) -> QCResult:
    """Apply rule-based QC to an hourly export.

    Rules (each rejected reading becomes NaN and is counted):
    * whole hour missing from the export (includes the spring daylight-saving hour);
    * flow < 0 or above `max_flow_mult` x the pump's spec flow;
    * kW < 0 or above `max_kw_mult` x the pump's spec kW;
    * discharge pressure < 0 while the pump runs (small negative offsets while off are normal);
    * section hours where every pump reads 0 (treated as a telemetry outage);
    * section production above `capacity_mult` x the sum of its pumps' spec flow.

    There is deliberately no statistical spike filter on production: production is
    an operating decision, and multi-hour steps (pumps parked overnight while tanks
    drain) are real. Statistical filters remove them, so only physical limits are used.
    """
    grid = pd.date_range(raw.index.min(), raw.index.max(), freq="h")
    df = raw.reindex(grid)
    missing_row = ~grid.isin(raw.index)

    pumps = [p for p in specs.index if f"{p}_FLOW" in df.columns]
    flow = pd.DataFrame(index=grid, columns=pumps, dtype=float)
    power = flow.copy()
    press = flow.copy()
    on_threshold = {}
    issues = {}
    nan_col = pd.Series(np.nan, index=grid)
    for p in pumps:
        f = df[f"{p}_FLOW"].astype(float)
        w = df.get(f"{p}_PowerUsage(kW-H)", nan_col).astype(float)
        pr = df.get(f"{p}_PRESS", nan_col).astype(float)
        # Fall back to the observed 99th percentile when the spec sheet has no value.
        spec_q = float(specs.at[p, "spec_flow_gpm"]) or float(f[f > 0].quantile(0.99))
        spec_kw = float(specs.at[p, "spec_kw"]) or float(w[w > 0].quantile(0.99))
        bad_f = (f < 0) | (f > max_flow_mult * spec_q)
        bad_w = (w < 0) | (w > max_kw_mult * spec_kw)
        on_threshold[p] = on_threshold_frac * spec_q
        running = f > on_threshold[p]
        bad_p = running & (pr < 0)
        issues[p] = {"bad_flow": int(bad_f.sum()), "bad_kw": int(bad_w.sum()), "bad_press": int(bad_p.sum()),
                     "flow_without_kw": int((running & (w <= 0)).sum())}
        flow[p] = f.mask(bad_f)
        power[p] = w.mask(bad_w)
        press[p] = pr.mask(bad_p)

    on = flow.gt(pd.Series(on_threshold), axis=1).astype(float).mask(flow.isna())

    production = pd.DataFrame(index=grid, columns=list(SECTIONS), dtype=float)
    flags = pd.DataFrame("", index=grid, columns=list(SECTIONS), dtype=object)
    filled = production.copy()
    imputed = pd.DataFrame(False, index=grid, columns=list(SECTIONS))
    summary: dict = {"hours": len(grid), "missing_rows": int(missing_row.sum()), "sections": {}}

    for sec in SECTIONS:
        cols = [p for p in pumps if specs.at[p, "section"] == sec]
        if not cols:
            continue
        sec_flow = flow[cols]
        total = sec_flow.sum(axis=1, min_count=len(cols))
        fl = pd.Series("", index=grid, dtype=object)
        fl = _append_flag(fl, pd.Series(missing_row, index=grid), "missing_row")
        unknown = sec_flow.isna().any(axis=1) & ~pd.Series(missing_row, index=grid)
        fl = _append_flag(fl, unknown, "invalid_pump_reading")
        all_zero = total == 0
        fl = _append_flag(fl, all_zero, "all_pumps_zero")
        capacity = float(specs.loc[cols, "spec_flow_gpm"].sum())
        above = total > capacity_mult * capacity
        fl = _append_flag(fl, above, "above_capacity")
        total = total.mask(all_zero | above)
        production[sec] = total
        flags[sec] = fl
        filled[sec], imputed[sec] = fill_for_model(total, short_gap_hours)
        summary["sections"][sec] = {
            "valid_hours": int(total.notna().sum()),
            "invalid_pump_reading": int(unknown.sum()),
            "all_pumps_zero": int(all_zero.sum()),
            "above_capacity": int(above.sum()),
            "imputed_for_models": int(imputed[sec].sum()),
        }

    steps = raw.index.to_series().diff()
    summary["dst_spring_gaps"] = [str(t) for t, s in steps.items()
                                  if s == pd.Timedelta(hours=2) and t.month == 3 and t.hour == 3]
    summary["long_gaps"] = [f"{t - s} -> {t}" for t, s in steps.items() if s > pd.Timedelta(hours=2)]

    tank = None
    if tank_columns:
        tank = pd.DataFrame({sec: df[col].astype(float) for sec, col in tank_columns.items() if col in df},
                            index=grid)

    pump_issues = pd.DataFrame(issues).T
    return QCResult(flow=flow, power=power, press=press, on=on, production=production,
                    production_filled=filled, imputed=imputed, flags=flags,
                    pump_issues=pump_issues, summary=summary, tank_mg=tank)


def reconstruct_demand(production_gpm: pd.Series, tank_mg: pd.Series | None) -> pd.Series:
    """Customer demand = production - change in storage.

    Without tank telemetry production is the only proxy for demand. That bakes
    the operators' tank-filling habits into the forecast, so add tank level tags
    (TNK_<section>_MG) to the export as soon as they are available.
    """
    if tank_mg is None or tank_mg.isna().all():
        return production_gpm
    d_storage_gpm = tank_mg.diff() * 1e6 / 60.0
    return production_gpm - d_storage_gpm


def run_lengths(on: pd.Series) -> pd.Series:
    """Hours each pump has been in its current state, per timestamp (NaN treated as off)."""
    state = on.fillna(0).astype(int)
    change = state.ne(state.shift()).cumsum()
    return state.groupby(change).cumcount() + 1
