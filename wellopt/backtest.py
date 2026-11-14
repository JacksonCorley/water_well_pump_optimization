"""Closed-loop backtest: replay history hour by hour under different operating policies.

Policies
* historical    - what the operators actually did (on/off states from SCADA).
* legacy_hourly - the original project's method: every hour, the cheapest set of
                  pumps whose flow covers that hour's actual production. It has
                  perfect hindsight, ignores storage and switches pumps freely.
* rule_<fc>     - storage-aware merit-order rule driven by forecast <fc>.
* milp_<fc>     - the MILP scheduler driven by forecast <fc>, re-planned every
                  `replan_every` hours (model predictive control).
<fc> is one of naive, gbm or perfect (perfect = actual demand, an upper bound).

Every policy is costed with the same pump power model and tariff. Without tank
telemetry, demand is taken to be historical production, so the historical and
legacy policies have flat storage by construction, while the storage-aware
policies use the configured tank. End-of-window storage differences are valued at
the policy's own average energy cost per MG so policies can't win by draining the tank.

The simulated tank is physical: it cannot rise above its usable volume or fall below
empty. Water pumped into a full tank is spilled (its energy still counts) and demand an
empty tank cannot meet is unserved; unserved water is charged at the policy's average
cost per MG so a policy can't win by not delivering water either.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import SiteConfig
from .data import QCResult, fill_for_model
from .forecast import PerfectForesight, make_forecaster
from .milp import MG_PER_GPM_HOUR
from .scheduler import plan_section, state_from_history
from .tariff import Tariff, kw_by_meter

BASE_POLICIES = ("historical", "legacy_hourly")


@dataclass
class SimResult:
    policy: str
    section: str
    on: pd.DataFrame          # hours x pumps, 0/1
    starts: pd.DataFrame      # hours x pumps, 1 where the pump started
    hourly: pd.DataFrame      # production, demand, storage, kW, cost
    metrics: dict = field(default_factory=dict)


def legacy_hourly_schedule(demand: np.ndarray, flow: np.ndarray, kw: np.ndarray,
                           must: np.ndarray | None = None) -> np.ndarray:
    """Cheapest pump subset covering each hour's demand (exact, by enumeration).

    Returns (P, T) 0/1. Hours whose demand exceeds total capacity run every pump.
    """
    P = len(flow)
    if P > 20:
        raise ValueError("subset enumeration is limited to 20 pumps per section")
    masks = ((np.arange(2 ** P)[:, None] >> np.arange(P)) & 1).astype(bool)
    if must is not None and np.any(must):
        masks = masks[masks[:, np.asarray(must, bool)].all(axis=1)]
    f, w = masks @ flow, masks @ kw
    order = np.argsort(f, kind="stable")
    f_sorted, w_sorted = f[order], w[order]
    best_from = np.empty(len(order), dtype=int)  # argmin of w over subsets with flow >= f_sorted[i]
    best = len(order) - 1
    for i in range(len(order) - 1, -1, -1):
        if w_sorted[i] <= w_sorted[best]:
            best = i
        best_from[i] = best
    pos = np.minimum(np.searchsorted(f_sorted, demand, side="left"), len(order) - 1)
    return masks[order[best_from[pos]]].T.astype(int)


def tank_step(level_mg: float, net_inflow_mg: float, usable_mg: float) -> tuple[float, float, float]:
    """One hour of a tank that cannot overfill or go below empty: (new level, spilled MG, unserved MG)."""
    raw = level_mg + net_inflow_mg
    return min(max(raw, 0.0), usable_mg), max(raw - usable_mg, 0.0), max(-raw, 0.0)


def _starts(on: np.ndarray, on0: np.ndarray) -> np.ndarray:
    prev = np.concatenate([on0[:, None], on[:, :-1]], axis=1)
    return (on > prev).astype(int)


def simulate(section: str, policy: str, start, end, *, qc: QCResult, table: pd.DataFrame, cfg: SiteConfig,
             tariff: Tariff, replan_every: int = 6, refit_every_days: int = 28, progress=None,
             forecaster_cache: dict | None = None) -> SimResult:
    """Simulate one policy for one section over [start, end)."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    hours = pd.date_range(start, end - pd.Timedelta(hours=1), freq="h")
    tbl = table[(table["section"] == section) & table["available"]]
    pumps = list(tbl.index)
    flow, kw = tbl["flow_gpm"].to_numpy(float), tbl["kw_plan"].to_numpy(float)
    demand = qc.production_filled[section].reindex(hours).to_numpy(float)
    on_hist = qc.on[pumps]
    on0 = on_hist.loc[: start - pd.Timedelta(hours=1)].iloc[-1].fillna(0).to_numpy(int)
    st = cfg.storage[section]
    solve_times, statuses = [], []
    spilled, unserved = np.zeros(len(hours)), np.zeros(len(hours))

    if policy == "historical":
        on = on_hist.reindex(hours).fillna(0).to_numpy(int).T
        production = demand.copy()
        storage = np.full(len(hours) + 1, st.initial_mg)
    elif policy == "legacy_hourly":
        on = legacy_hourly_schedule(demand, flow, kw, tbl["must_run"].to_numpy(bool))
        production = flow @ on
        storage = np.full(len(hours) + 1, st.initial_mg)  # surplus is spilled, as in the original method
        spilled = np.maximum(production - demand, 0.0) * MG_PER_GPM_HOUR
    else:
        method, kind = policy.split("_", 1)
        if method not in ("milp", "rule") or kind not in ("naive", "gbm", "perfect"):
            raise ValueError(f"unknown policy {policy!r}")
        y = qc.production[section]
        H = cfg.ops.horizon_hours
        cache = forecaster_cache if forecaster_cache is not None else {}
        forecaster, fitted_at = None, None
        on = np.zeros((len(pumps), len(hours)), dtype=int)
        storage = np.empty(len(hours) + 1)
        storage[0] = st.initial_mg
        # Real history before the window, then the simulated hours. 40 days always
        # covers the current billing month, so month-to-date peaks stay right.
        sim_on = on_hist.loc[: start - pd.Timedelta(hours=1)].iloc[-24 * 40:].fillna(0).astype(int)
        t0 = time.time()
        for k in range(0, len(hours), replan_every):
            now = hours[k]
            origin = now - pd.Timedelta(hours=1)
            state = state_from_history(sim_on, table, tariff, now, {section: float(storage[k])})
            if kind == "perfect":
                fc = PerfectForesight(qc.production_filled[section]).predict(None, origin, H)
            else:
                if fitted_at is None or now - fitted_at >= pd.Timedelta(days=refit_every_days):
                    fitted_at = now
                    key = (section, kind, now)
                    if key not in cache:  # shared by policies that use the same forecaster
                        hist = y.loc[:origin]
                        fill, imp = fill_for_model(hist)
                        cache[key] = make_forecaster(kind, cfg.forecast, H).fit(fill, valid=~imp & hist.notna())
                    forecaster = cache[key]
                fill, _ = fill_for_model(y.loc[:origin])
                fc = forecaster.predict(fill, origin, H)
            plan = plan_section(section, fc, state, table, cfg, tariff, method=method)
            solve_times.append(plan.summary["solve_s"])
            statuses.append(plan.status)
            n = min(replan_every, len(hours) - k)
            block = plan.schedule.reindex(columns=pumps).fillna(0).to_numpy(int)[:n].T
            on[:, k:k + n] = block
            for j in range(n):
                storage[k + j + 1], spilled[k + j], unserved[k + j] = tank_step(
                    storage[k + j], MG_PER_GPM_HOUR * (flow @ block[:, j] - demand[k + j]), st.usable_mg)
            sim_on = pd.concat([sim_on, pd.DataFrame(block.T, index=hours[k:k + n], columns=pumps)]).iloc[-24 * 40:]
            if progress and (k // replan_every) % 20 == 0:
                progress(f"{section} {policy}: {now:%Y-%m-%d %H:%M} ({time.time() - t0:.0f}s)")
        production = flow @ on

    starts = _starts(on, on0)
    kw_h = on.T @ kw
    price = tariff.energy_price(hours)
    hourly = pd.DataFrame({"demand_gpm": demand, "production_gpm": production, "storage_mg": storage[1:],
                           "spilled_mg": spilled, "unserved_mg": unserved,
                           "kw": kw_h, "price_usd_per_kwh": price, "energy_usd": kw_h * price,
                           "pumps_on": on.sum(axis=0), "starts": starts.sum(axis=0)}, index=hours)
    res = SimResult(policy, section, pd.DataFrame(on.T, index=hours, columns=pumps),
                    pd.DataFrame(starts.T, index=hours, columns=pumps), hourly)
    res.metrics = metrics(res, qc, table, cfg, tariff, start, storage[0], solve_times, statuses)
    return res


def metrics(res: SimResult, qc: QCResult, table: pd.DataFrame, cfg: SiteConfig, tariff: Tariff,
            start: pd.Timestamp, storage0: float, solve_times: list, statuses: list) -> dict:
    h = res.hourly
    st = cfg.storage[res.section]
    month_start = start.to_period("M").start_time
    pre = qc.on.loc[month_start: start - pd.Timedelta(hours=1), res.on.columns]
    prior = {}
    if not pre.empty:
        pk = kw_by_meter(pre, table)
        pk = pk[tariff.demand_hours(pk.index)]
        prior = pk.max().to_dict() if not pk.empty else {}
    bill = tariff.cost(kw_by_meter(res.on, table), prior_peak_kw=prior)
    on = res.on.to_numpy()
    phys_kw_h = on @ table.loc[res.on.columns, "kw_physics"].to_numpy()
    phys_kwh = float(phys_kw_h.sum())
    phys_usd = float((phys_kw_h * h["price_usd_per_kwh"]).sum())
    volume_mg = float(h["production_gpm"].sum() * MG_PER_GPM_HOUR)
    usd_per_mg = bill["energy_usd"] / volume_mg if volume_mg > 0 else np.nan
    d_storage = float(h["storage_mg"].iloc[-1] - storage0)
    unserved_mg = float(h["unserved_mg"].sum()) if "unserved_mg" in h else 0.0
    # Volume valued at the policy's average cost: refill what was drawn down, plus water not delivered.
    owed_mg = unserved_mg - d_storage
    # Volume the on/off states deliver at the planning flows. For "historical" this checks
    # that the per-pump mean flows reproduce what was actually pumped in the window.
    plan_flow_mg = float((on @ table.loc[res.on.columns, "flow_gpm"].to_numpy()).sum() * MG_PER_GPM_HOUR)
    starts_per_pump_day = res.starts.groupby(res.starts.index.normalize()).sum()
    storage_aware = res.policy not in BASE_POLICIES
    out = {
        "policy": res.policy, "section": res.section, "hours": len(h),
        "energy_kwh": bill["energy_kwh"], "energy_usd": bill["energy_usd"], "demand_usd": bill["demand_usd"],
        "total_usd": bill["total_usd"],
        "storage_change_mg": d_storage,
        "energy_usd_storage_adjusted": bill["energy_usd"] + owed_mg * usd_per_mg,
        "energy_kwh_storage_adjusted": bill["energy_kwh"] + owed_mg * (bill["energy_kwh"] / volume_mg
                                                                       if volume_mg > 0 else 0.0),
        "total_usd_storage_adjusted": bill["total_usd"] + owed_mg * usd_per_mg,
        "kwh_per_kgal": bill["energy_kwh"] / (volume_mg * 1000) if volume_mg > 0 else np.nan,
        "energy_kwh_physics": phys_kwh, "energy_usd_physics": phys_usd,
        "energy_kwh_physics_storage_adjusted": phys_kwh + owed_mg * (phys_kwh / volume_mg if volume_mg > 0 else 0.0),
        "demand_mg": float(h["demand_gpm"].sum() * MG_PER_GPM_HOUR), "pumped_mg": volume_mg,
        "pumped_mg_at_plan_flows": plan_flow_mg,
        "on_peak_kwh_share": float(h.loc[tariff.is_on_peak(h.index), "kw"].sum() / max(h["kw"].sum(), 1e-9)),
        "starts": int(h["starts"].sum()),
        "starts_per_pump_day_max": int(starts_per_pump_day.to_numpy().max()) if len(starts_per_pump_day) else 0,
        "mean_pumps_on": float(h["pumps_on"].mean()),
        "storage_min_mg": float(h["storage_mg"].min()) if storage_aware else np.nan,
        "storage_max_mg": float(h["storage_mg"].max()) if storage_aware else np.nan,
        "hours_below_min": int((h["storage_mg"] < st.min_mg).sum()) if storage_aware else 0,
        "hours_below_floor": int((h["storage_mg"] < st.floor_mg).sum()) if storage_aware else 0,
        "hours_empty": int((h["storage_mg"] <= 0).sum()) if storage_aware else 0,
        "hours_above_max": int((h["storage_mg"] > st.max_mg).sum()) if storage_aware else 0,
        "spilled_mg": float(h["spilled_mg"].sum()) if "spilled_mg" in h else 0.0,
        "unserved_mg": unserved_mg,
        "hours_spilling": int((h["spilled_mg"] > 1e-9).sum()) if storage_aware and "spilled_mg" in h else 0,
        "hours_unserved": int((h["unserved_mg"] > 1e-9).sum()) if "unserved_mg" in h else 0,
        "unmet_demand_hours": int((h["production_gpm"] < h["demand_gpm"] - 1e-6).sum()) if not storage_aware else 0,
        "replans": len(solve_times),
        "fallbacks": int(sum(s == "fallback" for s in statuses)),
        "time_limits": int(sum(s == "time_limit" for s in statuses)),
        "solve_s_mean": float(np.mean(solve_times)) if solve_times else np.nan,
        "solve_s_max": float(np.max(solve_times)) if solve_times else np.nan,
    }
    return out


def _timed_simulate(sec, pol, start, end, qc, table, cfg, tariff, replan_every, progress=None, cache=None):
    t0 = time.time()
    r = simulate(sec, pol, start, end, qc=qc, table=table, cfg=cfg, tariff=tariff, replan_every=replan_every,
                 progress=progress, forecaster_cache=cache)
    r.metrics["wall_s"] = round(time.time() - t0, 1)
    return r


_THREAD_LIMIT = None


def _limit_threads(n: int) -> None:
    """Worker initializer: share the cores between processes instead of every
    process starting one OpenMP thread per core (the GBM forecaster is multi-threaded)."""
    global _THREAD_LIMIT
    from threadpoolctl import threadpool_limits
    _THREAD_LIMIT = threadpool_limits(limits=n)  # keep a reference so the limit stays applied


def _cost_rank(policy: str) -> int:
    """Run the slow policies first so parallel workers finish together."""
    return 0 if policy.startswith("milp") else 1 if policy.startswith("rule") else 2


def run(sections, policies, start, end, *, qc, table, cfg, tariff, replan_every=6, progress=None, jobs=1):
    """Run every (section, policy) pair. Returns (metrics DataFrame, {(section, policy): SimResult}).

    jobs > 1 runs pairs in separate processes (each forecasting policy then fits its own model).
    """
    pairs = [(s, p) for s in sections for p in policies]
    results = {}
    if jobs > 1 and len(pairs) > 1:
        import os
        from concurrent.futures import ProcessPoolExecutor, as_completed
        workers = min(jobs, len(pairs))
        threads = max(1, (os.cpu_count() or 1) // workers)
        with ProcessPoolExecutor(max_workers=workers, initializer=_limit_threads, initargs=(threads,)) as ex:
            futures = {ex.submit(_timed_simulate, s, p, start, end, qc, table, cfg, tariff, replan_every): (s, p)
                       for s, p in sorted(pairs, key=lambda sp: _cost_rank(sp[1]))}
            for f in as_completed(futures):
                r = f.result()
                results[futures[f]] = r
                if progress:
                    progress(f"done {r.section} {r.policy} in {r.metrics['wall_s']}s")
        results = {k: results[k] for k in pairs}
    else:
        cache = {}  # policies that share a forecaster fit it once
        for s, p in pairs:
            results[(s, p)] = r = _timed_simulate(s, p, start, end, qc, table, cfg, tariff, replan_every,
                                                  progress, cache)
            if progress:
                progress(f"done {s} {p} in {r.metrics['wall_s']}s")
    table_out = pd.DataFrame([r.metrics for r in results.values()])
    hist = table_out[table_out["policy"] == "historical"].set_index("section")
    for out_col, col in (("savings_vs_historical_pct", "total_usd_storage_adjusted"),
                         ("energy_savings_vs_historical_pct", "energy_usd_storage_adjusted"),
                         ("kwh_savings_pct", "energy_kwh_storage_adjusted"),
                         ("kwh_savings_physics_pct", "energy_kwh_physics_storage_adjusted")):
        table_out[out_col] = [100 * (1 - getattr(row, col) / hist.loc[row.section, col])
                              if row.section in hist.index and hist.loc[row.section, col] else np.nan
                              for row in table_out.itertuples()]
    return table_out, results
