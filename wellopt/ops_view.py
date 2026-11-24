"""What the ops page shows, worked out without Dash.

For each section it takes the newest stored plan and the history the plan was made from, and works
out, for the plan's first hour:
* what to do with each pump: turn on, turn off, keep running, leave off, or out of service;
* where each running pump operates (flow, and head estimated from pressure), compared with its
  maker curve when one is on file and with its own history;
* the numbers on the section's cards: demand, what is running, what the planner recommends.

OpsData keeps the history, contexts and curves cached between page refreshes. Its `mark` method
saves an out-of-service mark in the plan store and re-plans the pump's section at once.
"""
from __future__ import annotations

import functools
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import SECTIONS, SiteConfig
from .context import Context, load_context
from .curves import Envelope, FieldFit, PumpCurve, check_point, envelope, fit_field_curve, load_pump_curves
from .data import load_history
from .live.store import PlanStore
from .pumps import FT_PER_PSI, total_dynamic_head
from .scheduler import SectionPlan, replan
from .tariff import Tariff

H1 = pd.Timedelta(hours=1)
# action key -> (glyph, label); the pump list is sorted in this order
ACTIONS = {
    "on": ("▲", "Turn on"),
    "off": ("▼", "Turn off"),
    "keep": ("●", "Keep running"),
    "leave": ("○", "Leave off"),
    "oos": ("✕", "Out of service"),
}
ACTION_ORDER = list(ACTIONS)
PLAN_STATUS = {"optimal": "optimal", "staged": "solved", "rule": "rule-based", "time_limit": "solver time limit",
               "fallback": "fallback rule plan"}
EMPTY = ("No plans yet. Start the planner with `python -m wellopt live`, or make one plan with "
         "`python -m wellopt plan`. This page shows the newest plan for each section.")
RISING_NOTE = ("Its history traces head rising with flow, which a fixed-speed pump cannot do. Likely causes: a "
               "variable-speed drive, or a static level or specific capacity on file that needs checking.")
NO_CURVE_NOTE = ("No maker curve on file. Add its points to config/pump_curves.csv to show its best-efficiency "
                 "point and preferred operating range.")


def _num(v, nd: int | None = 1):
    """A JSON number: None for missing or non-finite values, rounded to `nd` places (None keeps all)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, nd) if nd is not None else f


def _f(row, key: str, default: float = np.nan) -> float:
    try:
        return float(row.get(key, default))
    except (TypeError, ValueError):
        return default


def _bools(values: pd.Series | None, index) -> pd.Series:
    if values is None:
        return pd.Series(True, index=index)
    return values.map(lambda v: bool(v) if isinstance(v, (bool, np.bool_, int, float)) and v == v else False)


def pump_number(pump: str) -> int:
    digits = "".join(ch for ch in str(pump) if ch.isdigit())
    return int(digits) if digits else 10 ** 6


def pump_action(was_on: bool, planned: bool, out_of_service: bool) -> str:
    """The action for this hour: a pump out of service that is still running should be turned off."""
    if out_of_service:
        return "off" if was_on else "oos"
    if planned:
        return "keep" if was_on else "on"
    return "off" if was_on else "leave"


def next_change(schedule: pd.DataFrame, pump: str) -> str | None:
    """When the plan next switches the pump, e.g. 'on until Tue 14:00'."""
    if pump not in schedule.columns or schedule.empty:
        return None
    s = schedule[pump].fillna(0).astype(int)
    state = "on" if s.iloc[0] else "off"
    later = s.iloc[1:][s.iloc[1:] != s.iloc[0]]
    if later.empty:
        return f"{state} for the whole plan ({len(s)} h)"
    return f"{state} until {later.index[0]:%a %H:%M}"


def _envelope_json(env: Envelope | None) -> dict | None:
    if env is None:
        return None
    key = lambda q: f"p{round(q * 100)}"  # noqa: E731
    return {"hours": env.hours, "since": str(env.since),
            "flow": {key(q): round(v, 1) for q, v in env.flow.items()},
            "head": {key(q): round(v, 1) for q, v in env.head.items()},
            "cloud": [[round(float(q)), round(float(h))] for q, h in zip(env.sample_flow, env.sample_head)]}


def _maker_json(c: PumpCurve | None) -> dict | None:
    if c is None:
        return None
    eff = None if c.efficiency_pct is None else [_num(e, 1) for e in c.efficiency_pct]
    return {"flow": [round(float(q), 1) for q in c.flow_gpm], "head": [round(float(h), 1) for h in c.head_ft],
            "eff": eff, "source": c.source, "bep": _num(c.bep_gpm, 1)}


def _fit_json(fit: FieldFit | None, min_r2: float) -> dict | None:
    if fit is None:
        return None
    return {"h0": fit.h0, "k": fit.k, "r2": _num(fit.r2, 3), "hours": fit.hours, "flow_lo": round(fit.flow_lo, 1),
            "flow_hi": round(fit.flow_hi, 1), "credible": bool(fit.credible(min_r2)), "rises": bool(fit.rises)}


def _head_note(static: float, sc: float) -> str:
    if not np.isfinite(static):
        return "The spec sheet has no static level for this pump, so its head cannot be estimated."
    drawdown = (f"plus flow ÷ {sc:.1f} gpm/ft specific capacity" if np.isfinite(sc) and sc > 0
                else "with no drawdown (no specific capacity on file)")
    return (f"Head is estimated: static level {static:,.0f} ft, {drawdown}, plus pressure × {FT_PER_PSI}. "
            "No head is measured.")


def section_view(plan: SectionPlan, ctx: Context | None, curves: dict[str, PumpCurve], marks: dict[str, dict],
                 cfg: SiteConfig) -> dict:
    """Everything the page shows for one section, in plain JSON types.

    The actions compare the plan's first hour with the pumps running in the hour before it (the plan's
    own record of the plant state). Operating points come from that hour's readings in `ctx`; without a
    context the actions are still shown, without points or history."""
    sec = plan.section
    start = plan.timeline.index[0]
    t_read = start - H1
    table = plan.pump_table()
    if table.empty and ctx is not None:
        table = ctx.table[ctx.table["section"] == sec]
    if table.empty:
        table = pd.DataFrame(index=list(plan.schedule.columns))
    avail = _bools(table.get("available"), table.index)
    state = plan.inputs.get("state") or {}
    on0 = state.get("on") or {}
    left_out = set(state.get("unavailable") or [])
    first = plan.schedule.iloc[0] if len(plan.schedule) else pd.Series(dtype=float)
    qc = ctx.qc if ctx is not None else None
    have_row = qc is not None and t_read in qc.flow.index
    eff = pd.to_numeric(table["kwh_per_kgal"], errors="coerce") if "kwh_per_kgal" in table else \
        pd.Series(np.nan, index=table.index)
    ranks = eff.where(avail).rank(method="min")
    n_ranked = int(eff.where(avail).notna().sum())
    settings = cfg.curves
    rows, warnings, late_marks = [], [], []

    for p in table.index:
        row = table.loc[p]
        static, sc = _f(row, "static_ft"), _f(row, "specific_capacity_gpm_ft")
        running_now = q = psi = kw_now = h = None
        if have_row and p in qc.flow.columns:
            on_reading = qc.on.at[t_read, p]
            running_now = None if pd.isna(on_reading) else bool(on_reading)
            if running_now:
                q, psi, kw_now = (_num(qc.flow.at[t_read, p], None), _num(qc.press.at[t_read, p], None),
                                  _num(qc.power.at[t_read, p], None))
                h = _num(total_dynamic_head(q, psi, static, sc), None) if q is not None and psi is not None else None
        was_on = bool(on0.get(p, 0)) if on0 else bool(running_now)

        env = fit = None
        if qc is not None and p in qc.flow.columns and np.isfinite(static):
            run = qc.on[p].eq(1) & qc.flow[p].notna() & qc.press[p].notna()
            hist_q = qc.flow.loc[run, p]
            hist_h = total_dynamic_head(hist_q, qc.press.loc[run, p], static, sc)
            env, fit = envelope(hist_q, hist_h), fit_field_curve(hist_q, hist_h)
        maker = curves.get(p)
        fitted = fit.curve(p) if fit is not None and fit.credible(settings.fit_min_r2) else None
        if running_now and q is not None and h is not None:
            check = check_point(q, h, env, maker, fitted, settings)
        elif was_on or running_now:
            check = check_point(None, None, env, maker, fitted, settings)
        else:
            check = None

        mark = marks.get(p)
        if mark is not None:
            oos = True
            oos_why = ("Marked out of service" + (f" ({mark['note']})" if mark.get("note") else "")
                       + f" on {str(mark.get('updated_at', ''))[:16]}"
                       + (f" by {mark['updated_by']}" if mark.get("updated_by") else "") + ".")
            if bool(first.get(p, 0)) and p not in left_out:
                late_marks.append(p)
        elif not bool(avail.get(p, False)):
            oos = True
            oos_why = ("Out of service in config/site.toml." if p in cfg.out_of_service else
                       "Not planned: the history has no usable flow and power readings for it.")
        else:
            oos, oos_why = False, ""
        planned = bool(first.get(p, 0)) and not oos
        action = pump_action(was_on, planned, oos)
        rank = None if pd.isna(ranks.get(p, np.nan)) else int(ranks[p])
        merit = f"#{rank} of {n_ranked} by energy per gallon" if rank else ""
        if oos:
            why = oos_why + (" It is running now: turn it off." if was_on else "")
        elif action == "leave" and p in left_out:
            why = "Returned to service after this plan was made; the next plan can use it."
        else:
            why = {"on": "The planner starts it this hour", "off": "The planner stops it this hour",
                   "keep": "The planner keeps it running", "leave": "Not needed this hour"}[action]
            why += f" ({merit})." if merit else "."

        expected = None
        if action == "on":
            if env is not None:
                expected = [env.flow[0.5], env.head[0.5]]
            else:
                qa, pa = _f(row, "flow_gpm"), _f(row, "press_psi")
                ha = total_dynamic_head(qa, pa, static, sc) if np.isfinite(static) else np.nan
                if np.isfinite(qa) and np.isfinite(ha):
                    expected = [qa, float(ha)]
        notes = list(check.notes) if check else []
        if expected is not None:
            notes.append(f"If started, expect about {expected[0]:,.0f} gpm at {expected[1]:,.0f} ft "
                         f"({'its median running point' if env is not None else 'its average flow and pressure'}).")
        if fit is not None and fit.rises:
            notes.append(RISING_NOTE)
        if maker is None:
            notes.append(NO_CURVE_NOTE)
        else:
            if maker.bep_gpm is None:
                notes.append("Its maker curve has no efficiencies, so its best-efficiency point and preferred range "
                             "are not shown.")
            if maker.source:
                notes.append(f"Maker curve source: {maker.source}.")
        notes.append(_head_note(static, sc))

        rows.append({
            "id": p, "pump": p, "action": action, "glyph": ACTIONS[action][0], "label": ACTIONS[action][1],
            "why": why, "next": None if oos else next_change(plan.schedule, p),
            "was_on": was_on, "planned": planned, "running_now": running_now, "oos": oos, "mark": mark,
            "flow": _num(q, 0), "psi": _num(psi, 1), "head": _num(h, 0), "kw": _num(kw_now, 0),
            "kwh_per_kgal": _num(eff.get(p), 3), "rank": rank, "ranked": n_ranked,
            "flow_plan": _num(_f(row, "flow_gpm"), 0), "kw_plan": _num(_f(row, "kw_plan"), 1),
            "static_ft": _num(static, 1), "sc": _num(sc, 2),
            "check": None if check is None else {"label": check.label, "level": check.level,
                                                 "pct_bep": _num(check.pct_bep, 0)},
            "notes": notes,
            "env": _envelope_json(env), "maker": _maker_json(maker), "fit": _fit_json(fit, settings.fit_min_r2),
            "expected": None if expected is None else [_num(expected[0], 0), _num(expected[1], 0)],
        })

    rows.sort(key=lambda r: (ACTION_ORDER.index(r["action"]), pump_number(r["pump"]), r["pump"]))
    running = [r for r in rows if r["was_on"]]
    no_reading = [r["pump"] for r in running if r["flow"] is None or r["head"] is None]
    if have_row and no_reading:
        warnings.append(f"No usable reading at {t_read:%H:%M} for {', '.join(no_reading)}, so "
                        f"{'its point is' if len(no_reading) == 1 else 'their points are'} not shown.")
    if late_marks:
        one = len(late_marks) == 1
        it = "it" if one else "them"
        warnings.append(f"{', '.join(late_marks)} {'was' if one else 'were'} marked out of service after this plan "
                        f"was made. The next planning run leaves {it} out; until then the planner's numbers still "
                        f"count {it}.")
    tl0 = plan.timeline.iloc[0]
    demand_now = qc.production.at[t_read, sec] if have_row and sec in qc.production.columns else None
    s = plan.summary
    metrics = {
        "demand_p50": _num(tl0.get("demand_p50"), 0), "demand_lo": _num(tl0.get("demand_lo"), 0),
        "demand_hi": _num(tl0.get("demand_hi"), 0), "band": s.get("band", "p10-p90"),
        "produced_gpm": _num(demand_now, 0),
        "running_n": len(running),
        "running_gpm": _num(sum(r["flow"] for r in running if r["flow"] is not None), 0),
        "running_kw": _num(sum(_f(table.loc[r["pump"]], "kw_plan", 0.0) for r in running), 0),
        "running_no_reading": no_reading,
        "planned_n": int(tl0.get("pumps_on", 0)), "planned_gpm": _num(tl0.get("production_gpm"), 0),
        "planned_kw": _num(tl0.get("kw"), 0),
        "turn_on": [r["pump"] for r in rows if r["action"] == "on"],
        "turn_off": [r["pump"] for r in rows if r["action"] == "off"],
        "storage_mg": _num(s.get("storage_now_mg"), 2), "usable_mg": _num(s.get("usable_mg"), 2),
        "storage_measured": bool(s.get("storage_measured")), "storage_source": s.get("storage_source"),
    }
    return {
        "section": sec, "plan_id": s.get("plan_id"), "plan_start": str(start), "readings_at": str(t_read),
        "created_at": s.get("created_at"), "status": plan.status, "status_label": PLAN_STATUS.get(plan.status,
                                                                                                    str(plan.status)),
        "power_model": s.get("power_model"), "forecaster": s.get("forecaster"),
        "metrics": metrics, "rows": rows, "notes": list(plan.notes), "warnings": warnings,
    }


def _locked(method):
    """One OpsData call at a time: gunicorn serves the page on several threads, and the caches are plain dicts."""
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return run


@dataclass
class MarkResult:
    pump: str
    out_of_service: bool
    section: str | None
    replanned: bool
    message: str


class OpsData:
    """The ops page's data, cached between refreshes: the history (read once), one context per plan
    hour, the pump curve file (re-read when it changes) and the section views."""

    def __init__(self, cfg: SiteConfig, store_path: str | Path | None = None, history_path: str | Path | None = None,
                 keep_contexts: int = 2):
        self.cfg = cfg
        self.tariff = Tariff(cfg.tariff)
        self.store_path = Path(store_path) if store_path else cfg.path(cfg.live.store_path)
        self.history_path = Path(history_path) if history_path else cfg.path(cfg.history_path)
        self.keep_contexts = keep_contexts
        self._store: PlanStore | None = None
        self._raw: tuple[float, pd.DataFrame] | None = None
        self._contexts: dict[pd.Timestamp, Context] = {}
        self._curves: tuple[float | None, dict, str | None] | None = None
        self._views: dict[tuple, dict] = {}
        self._lock = threading.RLock()

    def store(self) -> PlanStore | None:
        if self._store is None and self.store_path.exists():
            self._store = PlanStore(self.store_path)
        return self._store

    def history(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame | None:
        """Export rows in [start, end): from the store's historian cache when it reaches the last hour
        (the PI runner keeps it), otherwise from the history CSV."""
        store = self.store()
        last = store.last_hourly_time() if store is not None else None
        if last is not None and last >= end - H1:
            raw = store.load_hourly(start, end)
            if not raw.empty:
                return raw
        if not self.history_path.exists():
            return None
        mtime = self.history_path.stat().st_mtime
        if self._raw is None or self._raw[0] != mtime:
            self._raw = (mtime, load_history(self.history_path))
        return self._raw[1].loc[start: end - H1]

    def context(self, plan_start) -> Context | None:
        """QC'd readings and the pump table for the lookback window before `plan_start`, as the runner saw them."""
        key = pd.Timestamp(plan_start)
        if key not in self._contexts:
            raw = self.history(key - pd.Timedelta(days=self.cfg.live.lookback_days), key)
            if raw is None or raw.empty:
                return None
            self._contexts[key] = load_context(cfg=self.cfg, raw=raw)
            while len(self._contexts) > self.keep_contexts:
                self._contexts.pop(next(iter(self._contexts)))
        return self._contexts[key]

    def curves(self) -> tuple[dict[str, PumpCurve], str | None]:
        """Maker curves from the curve file and an error message if it could not be read."""
        path = self.cfg.path(self.cfg.curves.path)
        mtime = path.stat().st_mtime if path.exists() else None
        if self._curves is None or self._curves[0] != mtime:
            try:
                self._curves = (mtime, load_pump_curves(path), None)
            except ValueError as exc:  # a typo: reported until the file changes
                self._curves = (mtime, {}, str(exc))
            except OSError as exc:  # e.g. held open in Excel: not cached, so the next refresh tries again
                return {}, f"{path.name} could not be read ({exc})"
        return self._curves[1], self._curves[2]

    @_locked
    def views(self) -> dict:
        """{"sections": {section: view}, "message": text when there is nothing to show}."""
        store = self.store()
        plans = store.latest_plans() if store is not None else {}
        if not plans:
            return {"sections": {}, "message": EMPTY}
        curves, curve_error = self.curves()
        marks = store.out_of_service()
        mark_key = tuple((p, m["updated_at"]) for p, m in marks.items())
        out = {}
        for sec in [s for s in SECTIONS if s in plans] + sorted(set(plans) - set(SECTIONS)):
            plan = plans[sec]
            key = (sec, plan.summary.get("plan_id"), mark_key, self._curves[0] if self._curves else None)
            if key not in self._views:
                history_error = None
                try:
                    ctx = self.context(plan.timeline.index[0])
                except Exception as exc:  # show the plan's actions even when the history cannot be read
                    ctx, history_error = None, f"{type(exc).__name__}: {exc}"
                view = section_view(plan, ctx, curves, marks, self.cfg)
                if ctx is None:
                    view["warnings"].append(
                        "The readings behind this plan could not be loaded"
                        + (f" ({history_error})" if history_error else f" (no history before {plan.timeline.index[0]})")
                        + ", so operating points and pump history are not shown.")
                if curve_error:
                    view["warnings"].append(f"No maker curves are shown: {curve_error}")
                self._views = {k: v for k, v in self._views.items() if k[0] != sec}
                self._views[key] = view
            out[sec] = self._views[key]
        return {"sections": out, "message": None}

    @_locked
    def mark(self, pump: str, out_of_service: bool, note: str = "", by: str = "ops page") -> MarkResult:
        """Save an out-of-service mark (or clear it), log it as an info alert, and re-plan the pump's
        section from its newest plan, so the advice changes at once. Later planning runs read the mark."""
        store = self.store()
        if store is None:
            raise RuntimeError(f"there is no plan store at {self.store_path} yet")
        plans = store.latest_plans()
        sec = next((s for s, pl in plans.items() if pump in pl.pump_table().index or pump in pl.schedule.columns),
                   None)
        note = (note or "").strip()
        store.set_out_of_service(pump, out_of_service, note, by)
        what = "marked out of service" if out_of_service else "returned to service"
        plan = plans.get(sec)
        store.add_alert(sec, "info", f"out_of_service:{pump}",
                        f"{pump} {what} from the ops page" + (f": {note}" if note and out_of_service else "") + ".",
                        plan_time=plan.timeline.index[0] if plan is not None else None, dedupe_hours=0)
        self._views.clear()
        if plan is None:
            return MarkResult(pump, out_of_service, None, False,
                              f"{pump} {what}. No stored plan includes it, so nothing was re-planned.")
        marked = set(store.out_of_service())
        pumps = set(plan.pump_table().index) | set(plan.schedule.columns)
        left_out = set((plan.inputs.get("state") or {}).get("unavailable") or [])
        t0 = time.perf_counter()
        try:
            new = replan(plan, self.cfg, self.tariff, out_of_service=sorted((marked & pumps) - left_out),
                         in_service=sorted(left_out - marked))
        except Exception as exc:  # the mark is saved either way; the next planning run uses it
            return MarkResult(pump, out_of_service, sec, False,
                              f"{pump} {what}. The {sec} plan could not be redone here ({exc}); the next planning "
                              f"run will {'leave it out' if out_of_service else 'consider it again'}.")
        for k in ("forecaster", "storage_source", "data_end"):
            if k in plan.summary:
                new.summary[k] = plan.summary[k]
        source = plan.summary.get("storage_source")
        if source:
            new.notes = [n + f" Source: {source}." if n.startswith("Storage is ASSUMED") else n for n in new.notes]
        new.notes.insert(1, f"Re-planned from the ops page after {pump} was {what}.")
        store.save_plans({sec: new}, data_end=plan.summary.get("data_end"), source="ops-page")
        self._views.clear()
        return MarkResult(pump, out_of_service, sec, True,
                          f"{pump} {what}. The {sec} plan was redone in {time.perf_counter() - t0:.1f} s.")
