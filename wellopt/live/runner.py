"""One advisory planning cycle, and the loop that repeats it.

Fail-safes, in the order they apply:
* a section with no pump telemetry for `stale_after_minutes` is not re-planned,
  and an alert says so (the app keeps showing the last plan, marked stale);
* pumps with repeated missing or rejected readings raise a warning;
* if the demand model is missing, too old or produces nonsense, the runner
  retrains it or falls back to the seasonal-naive forecast;
* if the MILP fails, the merit-order rule plan is used (see milp.solve);
* any other error is recorded against the run and raised as an alert, and the
  loop carries on with the next cycle.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import load_config
from ..context import load_context
from ..data import fill_for_model, load_history
from ..forecast import SeasonalNaive
from ..scheduler import SectionPlan, plan_section, state_from_history
from .models import demand_series, get_forecaster
from .sources import CsvReplaySource, make_pi_source
from .store import PlanStore

H1 = pd.Timedelta(hours=1)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class CycleResult:
    run_id: int
    plan_time: pd.Timestamp
    status: str                                 # ok | partial | stale | error
    message: str
    plans: dict = field(default_factory=dict)   # section -> SectionPlan
    alerts: list = field(default_factory=list)  # dicts from PlanStore.add_alert
    duration_s: float = 0.0


# ----------------------------------------------------------------------------- checks
def _section_pumps(ctx, section: str) -> list[str]:
    t = ctx.table
    return [p for p in t.index[(t["section"] == section) & t["available"]] if p in ctx.qc.flow.columns]


def data_freshness(ctx, now) -> dict[str, tuple[pd.Timestamp | None, float]]:
    """Per section: last hour with any pump telemetry before `now`, and its age in hours
    (0 when the previous hour has arrived)."""
    out = {}
    for sec in ctx.qc.production.columns:
        pumps = _section_pumps(ctx, sec)
        arriving = ctx.qc.flow.loc[: now - H1, pumps].notna().any(axis=1) if pumps else pd.Series(dtype=bool)
        last = arriving.index[arriving.to_numpy()][-1] if arriving.any() else None
        out[sec] = (last, float((now - H1 - last) / H1) if last is not None else float("inf"))
    return out


def recent_pump_issues(ctx, now, hours: int = 6, min_bad: int = 3) -> dict[str, list[str]]:
    """Pumps with `min_bad`+ missing or rejected readings in the last `hours` hours,
    counting only hours when the rest of the section was reporting."""
    out = {}
    for sec in ctx.qc.production.columns:
        pumps = _section_pumps(ctx, sec)
        f = ctx.qc.flow.loc[now - hours * H1: now - H1, pumps]
        f = f[f.notna().any(axis=1)]
        out[sec] = [p for p, n in f.isna().sum().items() if n >= min_bad]
    return out


def current_storage(ctx, section: str, now, prev_plan: SectionPlan | None = None,
                    override: float | None = None) -> tuple[float, bool, str]:
    """(storage MG at the start of `now`, measured?, where the number came from)."""
    st = ctx.cfg.storage[section]
    if override is not None:
        return float(override), False, "entered by the user"
    tank = ctx.qc.tank_mg
    if tank is not None and section in tank:
        s = tank[section].loc[: now - H1].dropna()
        if len(s) and now - H1 - s.index[-1] <= 2 * H1:
            return float(s.iloc[-1]), True, f"tank telemetry at {s.index[-1]:%Y-%m-%d %H:%M}"
    if prev_plan is not None:
        tl = prev_plan.timeline
        if now == tl.index[0]:
            return float(prev_plan.summary["storage_now_mg"]), False, "same as the previous plan for this hour"
        # Without telemetry, assume operators followed the last plan and demand matched its forecast.
        if (now - H1) in tl.index and now - tl.index[0] <= 6 * H1:
            return (float(tl.loc[now - H1, "storage_p50_mg"]), False,
                    f"carried forward from the plan made for {tl.index[0]:%a %H:%M}")
    return float(st.initial_mg), False, f"configured starting level ({st.initial_frac:.0%} of {st.usable_mg:.1f} MG)"


def _check_forecast(fc: pd.DataFrame, horizon: int, y: pd.Series) -> None:
    if len(fc) != horizon:
        raise ValueError(f"forecast has {len(fc)} hours, expected {horizon}")
    vals = fc.select_dtypes("number").to_numpy(float)
    if not np.isfinite(vals).all():
        raise ValueError("forecast contains NaN or inf")
    peak = np.nanmax(y.to_numpy(float)) if y.notna().any() else np.inf
    if (vals < 0).any() or (vals > 3 * peak).any():
        raise ValueError("forecast is outside the physically plausible range")


def forecast_section(ctx, section: str, now, kind: str = "gbm", *, log=None, save_models: bool = True):
    """48 h demand forecast from data before `now`. Returns (forecast, description, note or None)."""
    H = ctx.cfg.ops.horizon_hours
    origin = pd.Timestamp(now) - H1
    y = demand_series(ctx, section)
    # Pad to the origin so a late last hour is filled instead of shifting every lead time.
    y = y.reindex(pd.date_range(y.index[0], origin, freq="h"))
    fill, imputed = fill_for_model(y)
    note = None
    if kind != "naive":
        try:
            model, meta, _ = get_forecaster(ctx, section, kind, now, save_new=save_models, log=log)
            fc = model.predict(fill, origin, H)
            _check_forecast(fc, H, y)
            return fc, f"{kind} (trained through {pd.Timestamp(meta['trained_until']):%Y-%m-%d %H:%M})", None
        except Exception as exc:  # fall back rather than leave operators without a plan
            note = f"The {kind} demand forecast failed ({exc}); this plan uses the seasonal-naive forecast."
    model = SeasonalNaive(quantiles=ctx.cfg.forecast.quantiles, horizon=H).fit(fill, valid=~imputed & y.notna())
    fc = model.predict(fill, origin, H)
    _check_forecast(fc, H, y)
    return fc, "naive" if note is None else "naive (fallback)", note


# ----------------------------------------------------------------------------- planning
def make_plans(ctx, at=None, forecaster_kind: str = "gbm", method: str = "milp",
               storage_mg: dict[str, float] | None = None, store: PlanStore | None = None,
               sections=None, log=None, save_models: bool = True) -> dict[str, SectionPlan]:
    """Plan each section from the hour `at` (default: the hour after the data ends).
    Pumps marked out of service in the store (from the ops page) are left out."""
    at = pd.Timestamp(at).floor("h") if at is not None else ctx.qc.index.max() + H1
    sections = list(sections) if sections is not None else list(ctx.qc.production.columns)
    prev = store.latest_plans() if store is not None else {}
    marked = tuple(store.out_of_service()) if store is not None else ()
    on_hist = ctx.qc.on.loc[: at - H1].ffill(limit=3)  # a reading that is a few hours late keeps its last state
    plans = {}
    for sec in sections:
        mg, measured, source = current_storage(ctx, sec, at, prev.get(sec), (storage_mg or {}).get(sec))
        fc, described, note = forecast_section(ctx, sec, at, forecaster_kind, log=log, save_models=save_models)
        state = state_from_history(on_hist, ctx.table, ctx.tariff, at, {sec: mg}, storage_measured=measured,
                                   unavailable=marked)
        plan = plan_section(sec, fc, state, ctx.table, ctx.cfg, ctx.tariff, method=method)
        plan.summary.update({"forecaster": described, "storage_source": source,
                             "data_end": str(ctx.qc.index.max())})
        plan.notes = [n + f" Source: {source}." if n.startswith("Storage is ASSUMED") else n for n in plan.notes]
        if note:
            plan.notes.insert(1, note)
        plans[sec] = plan
    return plans


def plan_alerts(store: PlanStore, section: str, plan: SectionPlan, now) -> list[dict]:
    out = []
    for n in plan.notes:
        if n.startswith("ALERT:"):
            out.append(store.add_alert(section, "alert", "storage_floor", f"{section}: {n[len('ALERT: '):]}", now))
        elif n.startswith("Warning:"):
            out.append(store.add_alert(section, "warning", "storage_min", f"{section}: {n[len('Warning: '):]}", now))
    if plan.status == "fallback":
        out.append(store.add_alert(section, "warning", "milp_fallback", f"{section}: the optimizer failed "
                                   f"({plan.summary.get('message')}); the rule-based plan is shown instead.", now))
    if "fallback" in str(plan.summary.get("forecaster", "")):
        out.append(store.add_alert(section, "warning", "forecast_fallback",
                                   f"{section}: the demand forecast fell back to seasonal-naive.", now, dedupe_hours=24))
    return out


def send_webhook(cfg, alerts: list[dict], log=None) -> bool:
    """POST new alerts as JSON. The `text` field makes Slack and Teams incoming webhooks show them."""
    url = cfg.live.alert_webhook
    if not url or not alerts:
        return False
    try:
        import requests
        text = "\n".join(f"[{a['level'].upper()}] {a['message']}" for a in alerts)
        payload = {"text": f"{cfg.name}\n{text}",
                   "site": cfg.name,
                   "alerts": [{k: a.get(k) for k in ("section", "level", "key", "message", "plan_time")}
                              for a in alerts]}
        requests.post(url, json=payload, timeout=10).raise_for_status()
        return True
    except Exception as exc:
        (log or _log)(f"alert webhook failed: {exc}")
        return False


def run_once(ctx, store: PlanStore, now, *, source_name: str = "csv-replay", forecaster_kind: str = "gbm",
             method: str = "milp", log=None, weather=None) -> CycleResult:
    """Check the data, forecast, plan both sections, store everything and send new alerts."""
    log = log or _log
    t0 = time.time()
    now = pd.Timestamp(now).floor("h")
    run_id = store.start_run(now, source_name)
    alerts: list[dict] = []
    plans: dict = {}
    data_end = None
    try:
        data_end = ctx.qc.index.max()
        stale_hours = ctx.cfg.live.stale_after_minutes / 60
        fresh = data_freshness(ctx, now)
        ok = []
        for sec, (last, age) in fresh.items():
            if age > stale_hours:
                since = f"since {last:%Y-%m-%d %H:%M}" if last is not None else "in the lookback window"
                alerts.append(store.add_alert(sec, "alert", "stale_data", f"{sec}: no pump telemetry {since}; the "
                                              "plan was not updated. Check the SCADA/historian feed.", now))
            else:
                ok.append(sec)
        for sec, pumps in recent_pump_issues(ctx, now).items():
            for p in pumps:
                alerts.append(store.add_alert(sec, "warning", f"pump_data:{p}", f"{p}: missing or rejected readings "
                                              "in 3 or more of the last 6 hours; it is planned from its long-run "
                                              "averages. Check the instrument.", now, dedupe_hours=24))
        if weather is not None:
            try:
                store.save_weather(weather.forecast())
            except Exception as exc:  # weather is context only, never a reason to skip a plan
                log(f"weather fetch failed: {exc}")
        if ok:
            plans = make_plans(ctx, now, forecaster_kind, method, store=store, sections=ok, log=log)
            store.save_plans(plans, data_end=data_end, run_id=run_id)
            for sec, plan in plans.items():
                alerts += plan_alerts(store, sec, plan, now)
        status = "ok" if len(ok) == len(fresh) else ("partial" if ok else "stale")
        message = "; ".join(f"{s}: {p.status}, ${p.summary['energy_usd']:.0f} energy over {p.summary['hours']} h"
                            for s, p in plans.items()) or "no plans made"
    except Exception as exc:
        status, message = "error", f"{type(exc).__name__}: {exc}"
        log(traceback.format_exc())
        alerts.append(store.add_alert(None, "alert", "runner_error", f"Planning cycle failed: {message}", now))
    duration = time.time() - t0
    store.finish_run(run_id, status, message, data_end, round(duration, 2))
    send_webhook(ctx.cfg, [a for a in alerts if a.get("new") and a["level"] in ("warning", "alert")], log)
    return CycleResult(run_id, now, status, message, plans, alerts, duration)


def _report(res: CycleResult, log) -> None:
    new = [a for a in res.alerts if a.get("new")]
    log(f"{res.plan_time:%Y-%m-%d %H:%M} {res.status} in {res.duration_s:.1f}s: {res.message}"
        + (f" | {len(new)} new alert(s)" if new else ""))
    for a in new:
        log(f"  [{a['level']}] {a['message']}")


def _weather_client(cfg, log):
    live = cfg.live
    if not live.weather_enabled:
        return None
    try:
        from .weather import NWSHourly
        return NWSHourly(live.weather_lat, live.weather_lon, live.weather_user_agent, cfg.timezone)
    except Exception as exc:
        log(f"weather disabled: {exc}")
        return None


def prune_store(store: PlanStore, cfg, now, log=None) -> dict[str, int] | None:
    """Housekeeping after each cycle: drop old plans and historian rows the runner no longer reads."""
    try:
        days = cfg.live.plan_retention_days
        n = store.prune(plans_before=now - pd.Timedelta(days=days) if days > 0 else None,
                        hourly_before=now - pd.Timedelta(days=cfg.live.lookback_days + 7))
        if any(n.values()):
            (log or _log)(f"pruned {n['plans']} plan(s), {n['runs']} run(s), {n['hourly']} cached hour(s)")
        return n
    except Exception as exc:  # housekeeping must never stop planning
        (log or _log)(f"store pruning failed: {exc}")
        return None


def _failed_cycle(store: PlanStore, now, source: str, exc: Exception, cfg, log) -> None:
    message = f"{type(exc).__name__}: {exc}"
    log(traceback.format_exc())
    run_id = store.start_run(now, source)
    store.finish_run(run_id, "error", message)
    alert = store.add_alert(None, "alert", "runner_error", f"Planning cycle failed: {message}", now)
    send_webhook(cfg, [alert] if alert.get("new") else [], log)


def run_loop(config_path=None, history=None, *, source: str = "csv", once: bool = False, interval_min: int = 60,
             replay_start=None, steps: int = 0, forecaster_kind: str = "gbm", method: str = "milp", log=None) -> None:
    """Run cycles until stopped.

    csv: replay the export hour by hour, back to back, from `replay_start` (default:
         7 days before the data ends), for `steps` cycles (0 = until the data ends).
    pi:  real time. Each cycle fetches new historian rows into the store's cache,
         plans, then sleeps until `interval_min` after the hour (+5 min for late data).
    """
    log = log or _log
    cfg = load_config(config_path)
    store = PlanStore(cfg.path(cfg.live.store_path))
    lookback = pd.Timedelta(days=cfg.live.lookback_days)

    if source in ("csv", "csv-replay"):
        src = CsvReplaySource(load_history(history or cfg.path(cfg.history_path)), start=replay_start)
        limit = 1 if once else (steps or None)
        log(f"replaying from {src.now():%Y-%m-%d %H:%M}, "
            f"{'until the data ends' if limit is None else f'{limit} cycle(s)'}; store {store.path}")
        done = 0
        while not src.exhausted and (limit is None or done < limit):
            now = src.now()
            try:
                ctx = load_context(cfg=cfg, raw=src.fetch(now - lookback, now))
                _report(run_once(ctx, store, now, source_name=src.name, forecaster_kind=forecaster_kind,
                                 method=method, log=log), log)
            except Exception as exc:
                _failed_cycle(store, now, src.name, exc, cfg, log)
            prune_store(store, cfg, now, log)
            src.advance(1)
            done += 1
        return

    if source == "pi":
        src = make_pi_source(cfg)
        weather = _weather_client(cfg, log)
        while True:
            now = src.now()
            try:
                last = store.last_hourly_time()
                start = now - lookback if last is None else max(now - lookback, last - 3 * H1)  # re-read late rows
                chunk = pd.Timedelta(days=30)
                t = start
                while t < now:
                    store.upsert_hourly(src.fetch(t, min(t + chunk, now)))
                    t += chunk
            except Exception as exc:  # carry on: the stale-data check raises the alert
                log(f"historian fetch failed: {exc}")
            try:
                raw = store.load_hourly(now - lookback, now)
                if raw.empty:
                    raise RuntimeError("no historian data in the cache yet")
                ctx = load_context(cfg=cfg, raw=raw)
                _report(run_once(ctx, store, now, source_name=src.name, forecaster_kind=forecaster_kind,
                                 method=method, log=log, weather=weather), log)
            except Exception as exc:
                _failed_cycle(store, now, src.name, exc, cfg, log)
            prune_store(store, cfg, now, log)
            if once:
                return
            wake = now + pd.Timedelta(minutes=interval_min) + pd.Timedelta(minutes=5)
            time.sleep(max(30.0, (wake - pd.Timestamp.now(tz=cfg.timezone).tz_localize(None)).total_seconds()))
    raise ValueError(f"unknown source {source!r} (use 'csv' or 'pi')")
