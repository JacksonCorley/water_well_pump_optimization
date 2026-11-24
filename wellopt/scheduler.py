"""Turn a demand forecast and the current plant state into a pump plan for one section."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from .config import SiteConfig
from .data import run_lengths
from .milp import ScheduleProblem, ScheduleResult, solve
from .tariff import Tariff, kw_by_meter


@dataclass
class PlantState:
    """What the scheduler needs to know about 'now'."""
    start: pd.Timestamp                # first hour to schedule
    storage_mg: dict[str, float]       # usable storage per section
    on: pd.Series                      # pump -> 1/0 during the hour before `start`
    run_hours: pd.Series               # pump -> hours spent in that state
    month_peak_kw: dict[str, float]    # meter -> billed peak so far this month
    storage_measured: bool = False     # False when storage is assumed rather than telemetered
    unavailable: tuple[str, ...] = ()  # pumps tripped or locked out right now


def state_from_history(on_hist: pd.DataFrame, table: pd.DataFrame, tariff: Tariff, start: pd.Timestamp,
                       storage_mg: dict[str, float], storage_measured: bool = False,
                       unavailable: tuple[str, ...] = ()) -> PlantState:
    """Build the state at `start` from on/off history (rows before `start` only)."""
    start = pd.Timestamp(start)
    hist = on_hist.loc[: start - pd.Timedelta(hours=1)]
    if hist.empty:
        zeros = pd.Series(0, index=on_hist.columns)
        return PlantState(start, dict(storage_mg), zeros, zeros + 999, {}, storage_measured, unavailable)
    recent = hist.iloc[-24 * 14:]
    on = recent.iloc[-1].fillna(0).astype(int)
    run = pd.Series({p: int(run_lengths(recent[p]).iloc[-1]) for p in recent.columns})
    month = hist.loc[start.to_period("M").start_time:]
    peaks: dict[str, float] = {}
    if not month.empty:
        kw = kw_by_meter(month, table)
        billable = kw[tariff.demand_hours(kw.index)]
        if not billable.empty:
            peaks = {m: float(v) for m, v in billable.max().items()}
    return PlantState(start, dict(storage_mg), on, run, peaks, storage_measured, unavailable)


def _plain(value):
    """Plain JSON types: numpy scalars to Python, NaN/inf to None, containers recursively."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_plain(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return str(value)
    return value


@dataclass
class SectionPlan:
    section: str
    status: str
    schedule: pd.DataFrame          # hours x pumps, 1 = run
    timeline: pd.DataFrame          # hourly demand, production, storage paths, kW and cost
    summary: dict
    notes: list[str] = field(default_factory=list)
    # What the plan was solved from (plant state, the section's pump table), so the app can
    # re-solve a what-if without loading the history. Empty for plans stored by older versions.
    inputs: dict = field(default_factory=dict)

    def to_record(self) -> dict:
        """JSON-friendly form for the store and the app."""
        return {
            "section": self.section, "status": self.status, "summary": _plain(self.summary), "notes": self.notes,
            "index": [str(t) for t in self.timeline.index],
            "schedule": {p: self.schedule[p].astype(int).tolist() for p in self.schedule.columns},
            "timeline": {c: [None if pd.isna(v) else float(v) for v in self.timeline[c]] for c in self.timeline.columns},
            "inputs": _plain(self.inputs),
        }

    @classmethod
    def from_record(cls, rec: dict) -> "SectionPlan":
        idx = pd.DatetimeIndex(pd.to_datetime(rec["index"]))
        schedule = pd.DataFrame(rec["schedule"], index=idx)
        timeline = pd.DataFrame(rec["timeline"], index=idx).astype(float)
        return cls(rec["section"], rec["status"], schedule, timeline, dict(rec["summary"]), list(rec.get("notes", [])),
                   dict(rec.get("inputs") or {}))

    def pump_table(self) -> pd.DataFrame:
        """The section's pump table as it was when the plan was made (empty if not stored)."""
        pumps = self.inputs.get("pumps") or {}
        return pd.DataFrame.from_dict(pumps, orient="index") if pumps else pd.DataFrame()

    def forecast(self) -> pd.DataFrame:
        """The demand forecast the plan was solved against, rebuilt from the timeline."""
        lo, _, hi = str(self.summary.get("band", "p10-p90")).partition("-")
        tl = self.timeline
        return pd.DataFrame({lo or "p10": tl["demand_lo"], "p50": tl["demand_p50"], hi or "p90": tl["demand_hi"],
                             "scen_low": tl["scen_low"], "scen_high": tl["scen_high"]}, index=tl.index)


def _quantile_columns(forecast: pd.DataFrame) -> list[str]:
    return sorted((c for c in forecast.columns if re.fullmatch(r"p\d+", c)), key=lambda c: int(c[1:]))


def build_problem(section: str, forecast: pd.DataFrame, state: PlantState, table: pd.DataFrame,
                  cfg: SiteConfig, tariff: Tariff) -> ScheduleProblem:
    tbl = table[(table["section"] == section) & table["available"] & ~table.index.isin(state.unavailable)]
    idx = forecast.index
    qcols = _quantile_columns(forecast)
    p50 = forecast["p50"] if "p50" in forecast else forecast[qcols[len(qcols) // 2]]
    high = forecast["scen_high"] if "scen_high" in forecast else forecast[qcols[-1]]
    low = forecast["scen_low"] if "scen_low" in forecast else forecast[qcols[0]]
    st, ops = cfg.storage[section], cfg.ops
    month_id = np.array([(m - state.start.to_period("M")).n for m in idx.to_period("M")], dtype=int)
    meters = list(tbl["meter"])
    # A month that starts inside the horizon uses this month's peak as its reference,
    # so a short look-ahead never pays a whole month's demand charge.
    prior = {(m, int(mo)): float(state.month_peak_kw.get(m, 0.0)) for m in set(meters) for mo in np.unique(month_id)}
    groups = [((tbl["group"] == g).to_numpy(), float(spec["max_gpm"]))
              for g, spec in cfg.groups.items() if "max_gpm" in spec]
    return ScheduleProblem(
        pumps=list(tbl.index), flow_gpm=tbl["flow_gpm"].to_numpy(float), kw=tbl["kw_plan"].to_numpy(float),
        price=tariff.energy_price(idx), demand_p50=p50.to_numpy(float),
        demand_high=np.maximum(high.to_numpy(float), 0), demand_low=np.maximum(low.to_numpy(float), 0),
        storage0_mg=float(state.storage_mg[section]), min_mg=st.min_mg, floor_mg=st.floor_mg, max_mg=st.max_mg,
        end_mg=st.end_mg, on0=state.on.reindex(tbl.index).fillna(0).to_numpy(int),
        run_hours0=state.run_hours.reindex(tbl.index).fillna(999).to_numpy(int),
        min_up=ops.min_up_hours, min_down=ops.min_down_hours, max_starts_per_day=ops.max_starts_per_day,
        start_cost=ops.start_cost_usd, penalty_min=ops.penalty_below_min_usd_per_mg,
        penalty_floor=ops.penalty_below_floor_usd_per_mg, penalty_max=ops.penalty_above_max_usd_per_mg,
        penalty_end=ops.penalty_end_usd_per_mg, must_run=tbl["must_run"].to_numpy(bool), meters=meters,
        demand_rate=tariff.cfg.demand_charge_usd_per_kw_month, demand_mask=tariff.demand_hours(idx),
        month_id=month_id, prior_peak_kw=prior, group_limits=groups, risk_horizon=ops.risk_horizon_hours,
        time_limit_s=ops.solver_time_limit_s, mip_gap=ops.mip_gap, stage_hours=ops.stage_hours or None)


def plan_section(section: str, forecast: pd.DataFrame, state: PlantState, table: pd.DataFrame, cfg: SiteConfig,
                 tariff: Tariff, method: str = "milp", problem: ScheduleProblem | None = None) -> SectionPlan:
    """Plan one section. `forecast` is indexed by the hours to plan (first = state.start)."""
    pr = problem or build_problem(section, forecast, state, table, cfg, tariff)
    res = solve(pr, method)
    return package(section, forecast, state, pr, res, table, cfg)


def package(section: str, forecast: pd.DataFrame, state: PlantState, pr: ScheduleProblem, res: ScheduleResult,
            table: pd.DataFrame, cfg: SiteConfig) -> SectionPlan:
    idx = forecast.index
    qcols = _quantile_columns(forecast)
    schedule = pd.DataFrame(res.on.T, index=idx, columns=pr.pumps)
    prod = res.on.T @ pr.flow_gpm
    kw = res.on.T @ pr.kw
    timeline = pd.DataFrame({
        "demand_lo": forecast[qcols[0]].to_numpy(float),
        "demand_p50": pr.demand_p50,
        "demand_hi": forecast[qcols[-1]].to_numpy(float),
        "scen_low": pr.demand_low, "scen_high": pr.demand_high,
        "production_gpm": prod, "kw": kw, "price_usd_per_kwh": pr.price, "energy_usd": kw * pr.price,
        "storage_p50_mg": res.storage["p50"][1:],
        "storage_high_demand_mg": res.storage["high_demand"][1:],
        "storage_low_demand_mg": res.storage["low_demand"][1:],
        "pumps_on": res.on.sum(axis=0),
    }, index=idx)
    r = int(np.clip(pr.risk_horizon, 1, pr.H))
    summary = {
        "section": section, "start": str(idx[0]), "end": str(idx[-1]), "hours": int(pr.H), "status": res.status,
        "solve_s": round(res.solve_s, 3), "mip_gap": res.mip_gap, "message": res.message,
        "energy_kwh": float(kw.sum()), **{k: float(v) for k, v in res.cost.items()},
        "starts": int(res.starts), "pump_hours": int(res.on.sum()),
        "kwh_per_kgal": float(kw.sum() / (prod.sum() * 0.06)) if prod.sum() > 0 else None,
        "storage_now_mg": float(pr.storage0_mg), "storage_measured": bool(state.storage_measured),
        "min_mg": pr.min_mg, "floor_mg": pr.floor_mg, "max_mg": pr.max_mg, "end_mg": pr.end_mg,
        "usable_mg": float(cfg.storage[section].usable_mg), "risk_horizon_hours": r,
        "storage_low_point_mg": float(res.storage["high_demand"][1:r + 1].min()),
        "storage_high_point_mg": float(res.storage["low_demand"][1:r + 1].max()),
        "storage_end_p50_mg": float(res.storage["p50"][-1]),
        "power_model": cfg.power_source, "band": f"{qcols[0]}-{qcols[-1]}",
    }
    notes = make_notes(state, pr, res, schedule, table, cfg, summary)
    return SectionPlan(section, res.status, schedule, timeline, summary, notes, plan_inputs(section, state, table))


def plan_inputs(section: str, state: PlantState, table: pd.DataFrame) -> dict:
    """The parts of the plant state and pump table a plan depends on, in plain JSON types."""
    sec_table = table[table["section"] == section]
    pumps = list(sec_table.index)
    meters = set(sec_table["meter"])
    return _plain({
        "state": {
            "on": state.on.reindex(pumps).fillna(0).astype(int).to_dict(),
            "run_hours": state.run_hours.reindex(pumps).fillna(999).astype(int).to_dict(),
            "month_peak_kw": {m: v for m, v in state.month_peak_kw.items() if m in meters},
            "unavailable": [p for p in state.unavailable if p in pumps],
            "storage_measured": bool(state.storage_measured),
        },
        "pumps": sec_table.to_dict("index"),
    })


def replan(plan: SectionPlan, cfg: SiteConfig, tariff: Tariff, *, storage_mg: float | None = None,
           out_of_service=(), in_service=(), method: str = "milp", time_limit_s: float | None = 10.0) -> SectionPlan:
    """Re-solve a stored plan with a different storage level or different pumps out of service (a what-if,
    or the ops page after a pump is marked out of service or returned to service).

    `out_of_service` adds pumps to those the plan left out; `in_service` puts back pumps the plan left out
    (pumps unavailable in the pump table, such as those with no data, stay out). Uses only what the plan
    stored, so it gives the same answer wherever it runs. `time_limit_s` caps each solver stage so an
    interactive request stays quick."""
    if not plan.inputs.get("pumps"):
        raise ValueError("this plan was stored without its inputs; make a new plan to run a what-if")
    st = plan.inputs["state"]
    table = plan.pump_table()
    for col in ("available", "must_run"):
        table[col] = table[col].fillna(False).astype(bool)
    sec = plan.section
    mg = float(plan.summary["storage_now_mg"]) if storage_mg is None else float(storage_mg)
    state = PlantState(start=plan.timeline.index[0], storage_mg={sec: mg},
                       on=pd.Series(st["on"], dtype=int), run_hours=pd.Series(st["run_hours"], dtype=int),
                       month_peak_kw=dict(st.get("month_peak_kw") or {}),
                       storage_measured=bool(st.get("storage_measured")) and storage_mg is None,
                       unavailable=tuple(p for p in dict.fromkeys([*st.get("unavailable", []), *out_of_service])
                                         if p not in set(in_service)))
    if time_limit_s:
        cfg = replace(cfg, ops=replace(cfg.ops, solver_time_limit_s=min(cfg.ops.solver_time_limit_s, time_limit_s)))
    return plan_section(sec, plan.forecast(), state, table, cfg, tariff, method=method)


def _fmt_t(t: pd.Timestamp) -> str:
    return t.strftime("%a %H:%M")


def make_notes(state: PlantState, pr: ScheduleProblem, res: ScheduleResult, schedule: pd.DataFrame,
               table: pd.DataFrame, cfg: SiteConfig, summary: dict) -> list[str]:
    """Plain-language notes for operators, most important first."""
    notes = []
    first = schedule.iloc[0]
    on0 = pd.Series(pr.on0, index=pr.pumps)
    starts = [p for p in pr.pumps if first[p] == 1 and on0[p] == 0]
    stops = [p for p in pr.pumps if first[p] == 0 and on0[p] == 1]
    left_out = [p for p in state.unavailable if p in table.index and table.at[p, "section"] == summary["section"]]
    # a pump taken out of service while it runs must be stopped too, though the problem no longer includes it
    stops += [f"{p} (out of service)" for p in left_out if int(state.on.get(p, 0)) == 1]
    running = [p for p in pr.pumps if first[p] == 1]
    when = _fmt_t(schedule.index[0])
    if starts or stops:
        parts = ([f"start {', '.join(starts)}"] if starts else []) + ([f"stop {', '.join(stops)}"] if stops else [])
        notes.append(f"{when}: {'; '.join(parts)}. {len(running)} pumps running.")
    else:
        notes.append(f"{when}: no change. {len(running)} pumps running: {', '.join(running) or 'none'}.")
    changes = []
    diff = schedule.astype(int).diff().iloc[1:13]
    for t, row in diff.iterrows():
        for p, v in row.items():
            if v:
                changes.append(f"{'start' if v > 0 else 'stop'} {p} at {_fmt_t(t)}")
    if changes:
        notes.append("Next 12 h: " + "; ".join(changes[:8]) + ("; ..." if len(changes) > 8 else "") + ".")
    if left_out:
        notes.append(f"Planned without {', '.join(left_out)} (out of service for this plan).")

    if res.status in ("fallback", "rule"):
        notes.append(f"Rule-based plan ({res.message or 'requested'}); it ignores tariff timing and start limits.")
    elif res.status == "time_limit":
        staged = bool(pr.stage_hours) and pr.stage_hours < pr.H
        notes.append("Solver hit its time limit" + (" in at least one stage" if staged else "")
                     + f"; plan is feasible but may be up to {100 * (res.mip_gap or 0):.1f}% from optimal"
                     + (" within that stage." if staged else "."))
    if not state.storage_measured:
        notes.append(f"Storage is ASSUMED at {pr.storage0_mg:.2f} MG (no tank telemetry). Add tank level tags before "
                     "relying on the storage plan.")
    r = summary["risk_horizon_hours"]
    low, high = summary["storage_low_point_mg"], summary["storage_high_point_mg"]
    if low < pr.floor_mg:
        notes.append(f"ALERT: at high demand, storage could fall to {low:.2f} MG within {r} h, below the floor "
                     f"({pr.floor_mg:.2f} MG), even with the planned pumps. Check capacity and out-of-service pumps.")
    elif low < pr.min_mg:
        notes.append(f"Warning: at high demand, storage could dip to {low:.2f} MG within {r} h (minimum "
                     f"{pr.min_mg:.2f} MG).")
    else:
        notes.append(f"At high demand, storage stays at or above {low:.2f} MG over the next {r} h (minimum "
                     f"{pr.min_mg:.2f} MG).")
    if high > pr.max_mg:
        notes.append(f"At low demand, storage could reach {high:.2f} MG (max {pr.max_mg:.2f} MG); the next re-plan "
                     "will cut pumping.")
    unused = [p for p in pr.pumps if schedule[p].sum() == 0]
    if unused:
        eff = table.loc[unused, "kwh_per_kgal"].sort_values(ascending=False)
        notes.append("Not used in this plan: " + ", ".join(f"{p} ({v:.3f} kWh/kgal)" for p, v in eff.items()) + ".")
    if cfg.power_source == "recorded":
        notes.append("Costs use recorded kW, which fails the physics check (see data audit). Treat $ as relative.")
    return notes
