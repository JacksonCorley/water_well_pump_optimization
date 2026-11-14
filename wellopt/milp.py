"""Mixed-integer pump schedule over a rolling horizon, solved with HiGHS via scipy.

Decision: u[p, t] = 1 if pump p runs during hour t.

Objective = time-of-use energy cost
          + start cost per pump start
          + incremental demand charge (kW above the peak already billed this month)
          + penalties for storage outside its limits.

Storage is tracked for the median demand path. Running low is a service risk,
so the high-demand path (upper end of the cumulative demand band) must stay above
the minimum and floor levels for the first `risk_horizon` hours, and at its
risk-horizon offset after that. Running high only wastes energy and the next
re-plan corrects it, so only the median path is held below the maximum. The
high-demand path differs from the median path by a known amount each hour, so one
storage variable per hour covers both. Every storage limit is soft: the model is
always feasible, and a shortfall shows up as a penalty and a note rather than as a
solver failure.

Operating rules: minimum up and down times (including the state the pumps are in
at the start), maximum starts per pump in each 24-hour block of the plan (starts
made before the plan are not counted), must-run and out-of-service pumps, and
optional flow limits per pump group (shared header or treatment).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_array

MG_PER_GPM_HOUR = 60.0 / 1e6


@dataclass
class ScheduleProblem:
    pumps: list[str]
    flow_gpm: np.ndarray      # (P,) flow of each pump when running
    kw: np.ndarray            # (P,) power of each pump when running
    price: np.ndarray         # (H,) $/kWh
    demand_p50: np.ndarray    # (H,) gpm
    demand_high: np.ndarray   # (H,) gpm, high-demand path (upper cumulative quantile)
    demand_low: np.ndarray    # (H,) gpm, low-demand path (lower cumulative quantile)
    storage0_mg: float
    min_mg: float
    floor_mg: float
    max_mg: float
    end_mg: float
    on0: np.ndarray           # (P,) 1 if running in the hour before the plan
    run_hours0: np.ndarray    # (P,) hours spent in that state
    min_up: int = 4
    min_down: int = 2
    max_starts_per_day: int = 2
    start_cost: float = 10.0
    penalty_min: float = 20_000.0
    penalty_floor: float = 100_000.0
    penalty_max: float = 5_000.0
    penalty_end: float = 10_000.0
    must_run: np.ndarray | None = None
    force_off: np.ndarray | None = None
    meters: list[str] | None = None        # meter of each pump (default: one meter per pump)
    demand_rate: float = 0.0               # $/kW-month
    demand_mask: np.ndarray | None = None  # (H,) hours that count toward the billed peak
    month_id: np.ndarray | None = None     # (H,) 0 = current billing month, 1 = next, ...
    prior_peak_kw: dict = field(default_factory=dict)  # {(meter, month_id): kW already billed}
    group_limits: list = field(default_factory=list)   # [(member mask (P,), max gpm)]
    risk_horizon: int = 6
    time_limit_s: float = 20.0
    mip_gap: float = 0.005
    stage_hours: int | None = None   # relax-and-fix block length; None = solve the whole horizon at once

    @property
    def P(self) -> int:
        return len(self.pumps)

    @property
    def H(self) -> int:
        return len(self.price)

    def masks(self) -> tuple[np.ndarray, np.ndarray]:
        must = self.must_run if self.must_run is not None else np.zeros(self.P, bool)
        off = self.force_off if self.force_off is not None else np.zeros(self.P, bool)
        return np.asarray(must, bool), np.asarray(off, bool)

    def band_shifts(self) -> tuple[np.ndarray, np.ndarray]:
        """How far (MG) the high- and low-demand paths sit below / above the median
        path after each hour. The band stops widening after `risk_horizon` hours
        because the plan is recomputed long before then."""
        hi = np.cumsum(self.demand_high - self.demand_p50) * MG_PER_GPM_HOUR
        lo = np.cumsum(self.demand_p50 - self.demand_low) * MG_PER_GPM_HOUR
        r = int(np.clip(self.risk_horizon, 1, self.H))
        hi[r:] = hi[r - 1]
        lo[r:] = lo[r - 1]
        return np.maximum(hi, 0.0), np.maximum(lo, 0.0)


@dataclass
class ScheduleResult:
    status: str               # optimal | time_limit | rule | fallback
    on: np.ndarray            # (P, H) 0/1
    storage: dict             # name -> (H + 1,) MG, index 0 = now
    cost: dict                # energy_usd, start_usd, demand_usd, penalty_usd, total_usd
    starts: int
    solve_s: float
    mip_gap: float | None = None
    message: str = ""


class _Rows:
    """Accumulates sparse constraint rows lb <= A x <= ub."""

    def __init__(self) -> None:
        self.r: list[int] = []
        self.c: list[int] = []
        self.v: list[float] = []
        self.lb: list[float] = []
        self.ub: list[float] = []

    def add(self, cols, vals, lb: float, ub: float) -> None:
        n = len(self.lb)
        self.r.extend([n] * len(cols))
        self.c.extend(int(c) for c in cols)
        self.v.extend(float(v) for v in vals)
        self.lb.append(lb)
        self.ub.append(ub)

    def constraint(self, n_vars: int) -> LinearConstraint:
        a = coo_array((self.v, (self.r, self.c)), shape=(len(self.lb), n_vars)).tocsr()
        return LinearConstraint(a, np.array(self.lb), np.array(self.ub))


def initial_locks(pr: ScheduleProblem) -> tuple[np.ndarray, np.ndarray]:
    """(P, H) lower / upper bounds on u from must-run, out-of-service and the
    minimum up / down time still owed from before the plan starts."""
    must, off = pr.masks()
    lo = np.zeros((pr.P, pr.H))
    hi = np.ones((pr.P, pr.H))
    for p in range(pr.P):
        if pr.on0[p] and pr.run_hours0[p] < pr.min_up:
            lo[p, : int(pr.min_up - pr.run_hours0[p])] = 1
        if not pr.on0[p] and pr.run_hours0[p] < pr.min_down:
            hi[p, : int(pr.min_down - pr.run_hours0[p])] = 0
        if must[p]:
            lo[p, :] = 1
        if off[p]:
            hi[p, :] = 0
    return np.minimum(lo, hi), hi


def solve_milp(pr: ScheduleProblem) -> ScheduleResult | None:
    """Solve the scheduling MILP. Returns None if HiGHS finds no solution in time."""
    t0 = time.perf_counter()
    P, H, c = pr.P, pr.H, MG_PER_GPM_HOUR
    n_u = P * H
    i_s, i_d, i_v = n_u, 2 * n_u, 3 * n_u
    i_umin, i_ufloor, i_over = i_v + H, i_v + 2 * H, i_v + 3 * H
    i_end = i_v + 4 * H
    meters = list(pr.meters) if pr.meters is not None else list(pr.pumps)
    month_id = np.asarray(pr.month_id if pr.month_id is not None else np.zeros(H, int), int)
    dmask = np.asarray(pr.demand_mask if pr.demand_mask is not None else np.ones(H, bool), bool)
    keys = sorted({(m, int(mo)) for m in set(meters) for mo in np.unique(month_id[dmask])}) \
        if pr.demand_rate > 0 else []
    i_z = i_end + 1
    n_vars = i_z + len(keys)

    def u(p: int, t: int) -> int:
        return p * H + t

    cost = np.zeros(n_vars)
    cost[:n_u] = (pr.kw[:, None] * pr.price[None, :]).ravel()
    cost[i_s:i_s + n_u] = pr.start_cost
    cost[i_umin:i_umin + H] = pr.penalty_min
    cost[i_ufloor:i_ufloor + H] = pr.penalty_floor
    cost[i_over:i_over + H] = pr.penalty_max
    cost[i_end] = pr.penalty_end
    cost[i_z:] = pr.demand_rate

    lb = np.zeros(n_vars)
    ub = np.ones(n_vars)
    lock_lo, lock_hi = initial_locks(pr)
    lb[:n_u], ub[:n_u] = lock_lo.ravel(), lock_hi.ravel()
    lb[i_v:i_v + H], ub[i_v:i_v + H] = -np.inf, np.inf
    ub[i_umin:] = np.inf  # slacks and demand-charge peaks are >= 0
    integrality = np.zeros(n_vars)
    integrality[:n_u] = 1

    rows = _Rows()
    # Storage balance for the median path: V[k] is storage after hour k.
    for k in range(H):
        cols = [i_v + k] + [u(p, k) for p in range(P)]
        vals = [1.0] + list(-c * pr.flow_gpm)
        rhs = -c * pr.demand_p50[k]
        if k == 0:
            rhs += pr.storage0_mg
        else:
            cols.append(i_v + k - 1)
            vals.append(-1.0)
        rows.add(cols, vals, rhs, rhs)
    shift_hi, _ = pr.band_shifts()
    for k in range(H):
        rows.add([i_v + k, i_umin + k], [1, 1], pr.min_mg + shift_hi[k], np.inf)
        rows.add([i_v + k, i_ufloor + k], [1, 1], pr.floor_mg + shift_hi[k], np.inf)
        rows.add([i_v + k, i_over + k], [1, -1], -np.inf, pr.max_mg)
    rows.add([i_v + H - 1, i_end], [1, 1], pr.end_mg, np.inf)

    for p in range(P):
        for t in range(H):
            # u[t] - u[t-1] = start - stop (the tight unit-commitment form)
            if t == 0:
                rows.add([i_s + u(p, 0), i_d + u(p, 0), u(p, 0)], [1, -1, -1], -float(pr.on0[p]), -float(pr.on0[p]))
            else:
                rows.add([i_s + u(p, t), i_d + u(p, t), u(p, t), u(p, t - 1)], [1, -1, -1, 1], 0.0, 0.0)
            if pr.min_up > 1:  # a start in the last min_up hours means the pump is on now
                a = max(0, t - pr.min_up + 1)
                rows.add([i_s + u(p, j) for j in range(a, t + 1)] + [u(p, t)],
                         [1] * (t + 1 - a) + [-1], -np.inf, 0.0)
            if pr.min_down > 1:  # a stop in the last min_down hours means the pump is off now
                b = max(0, t - pr.min_down + 1)
                rows.add([i_d + u(p, j) for j in range(b, t + 1)] + [u(p, t)],
                         [1] * (t + 1 - b) + [1], -np.inf, 1.0)
        if pr.max_starts_per_day > 0:
            for a in range(0, H, 24):
                cols = [i_s + u(p, t) for t in range(a, min(H, a + 24))]
                rows.add(cols, [1] * len(cols), -np.inf, float(pr.max_starts_per_day))

    if keys:
        kidx = {k: i_z + j for j, k in enumerate(keys)}
        for m in sorted(set(meters)):
            members = [p for p in range(P) if meters[p] == m]
            for t in np.flatnonzero(dmask):
                key = (m, int(month_id[t]))
                prior = float(pr.prior_peak_kw.get(key, 0.0))
                rows.add([u(p, t) for p in members] + [kidx[key]], list(pr.kw[members]) + [-1.0], -np.inf, prior)

    for mask, max_gpm in pr.group_limits:
        members = np.flatnonzero(mask)
        if len(members):
            for t in range(H):
                rows.add([u(p, t) for p in members], list(pr.flow_gpm[members]), -np.inf, float(max_gpm))

    # Relax-and-fix: hours [a, b) are integer, later hours are relaxed to [0, 1];
    # the solved block is then fixed and the next block made integer. Keeping
    # every pump in its last fixed state is always feasible, so each stage
    # has a solution. One stage = the exact MILP.
    constraint = rows.constraint(n_vars)
    stage = int(pr.stage_hours) if pr.stage_hours and pr.stage_hours < H else H
    n_stages = -(-H // stage)
    integrality = np.zeros(n_vars)
    gaps, all_optimal, message = [], True, ""
    for a in range(0, H, stage):
        cols = (np.arange(P)[:, None] * H + np.arange(a, min(H, a + stage))[None, :]).ravel()
        integrality[:] = 0
        integrality[cols] = 1
        res = milp(cost, integrality=integrality, bounds=Bounds(lb, ub), constraints=constraint,
                   options={"time_limit": float(pr.time_limit_s) / n_stages, "mip_rel_gap": float(pr.mip_gap),
                            "disp": False, "presolve": True})
        if res.x is None:
            return None
        lb[cols] = ub[cols] = np.round(res.x[cols])
        gaps.append(float(getattr(res, "mip_gap", 0.0) or 0.0))
        all_optimal &= res.status == 0
        message = str(res.message)
    solve_s = time.perf_counter() - t0
    on = np.round(lb[:n_u]).reshape(P, H).astype(int)
    out = evaluate_schedule(pr, on)
    status = ("optimal" if n_stages == 1 else "staged") if all_optimal else "time_limit"
    return ScheduleResult(status=status, on=on, storage=out["storage"], cost=out["cost"], starts=out["starts"],
                          solve_s=solve_s, mip_gap=max(gaps), message=message)


def evaluate_schedule(pr: ScheduleProblem, on: np.ndarray) -> dict:
    """Storage paths, starts and the same cost terms the MILP minimizes, for any schedule."""
    c = MG_PER_GPM_HOUR
    on = np.asarray(on, int)
    prod = pr.flow_gpm @ on
    storage = {
        name: pr.storage0_mg + np.concatenate([[0.0], np.cumsum((prod - d) * c)])
        for name, d in (("p50", pr.demand_p50), ("high_demand", pr.demand_high), ("low_demand", pr.demand_low))
    }
    prev = np.concatenate([np.asarray(pr.on0, int)[:, None], on[:, :-1]], axis=1)
    starts = int((on > prev).sum())
    energy = float((pr.price * (pr.kw @ on)).sum())
    demand_usd = 0.0
    if pr.demand_rate > 0:
        meters = list(pr.meters) if pr.meters is not None else list(pr.pumps)
        month_id = np.asarray(pr.month_id if pr.month_id is not None else np.zeros(pr.H, int), int)
        dmask = np.asarray(pr.demand_mask if pr.demand_mask is not None else np.ones(pr.H, bool), bool)
        for m in set(meters):
            members = [p for p in range(pr.P) if meters[p] == m]
            load = pr.kw[members] @ on[members]
            for mo in np.unique(month_id[dmask]):
                sel = dmask & (month_id == mo)
                peak = float(load[sel].max()) if sel.any() else 0.0
                demand_usd += max(0.0, peak - float(pr.prior_peak_kw.get((m, int(mo)), 0.0))) * pr.demand_rate
    shift_hi, _ = pr.band_shifts()
    v = storage["p50"][1:]
    penalty = (pr.penalty_min * np.maximum(0, pr.min_mg + shift_hi - v).sum()
               + pr.penalty_floor * np.maximum(0, pr.floor_mg + shift_hi - v).sum()
               + pr.penalty_max * np.maximum(0, v - pr.max_mg).sum()
               + pr.penalty_end * max(0.0, pr.end_mg - v[-1]))
    start_usd = pr.start_cost * starts
    return {"storage": storage, "starts": starts,
            "cost": {"energy_usd": energy, "start_usd": start_usd, "demand_usd": demand_usd,
                     "penalty_usd": float(penalty), "total_usd": energy + start_usd + demand_usd + float(penalty)}}


def rule_plan(pr: ScheduleProblem, close_gap_hours: float = 6.0) -> np.ndarray:
    """Storage-aware merit-order rule (the fallback, and a baseline policy).

    Each hour it aims to produce median demand plus enough to close the gap to a
    target level over `close_gap_hours`. The target is midway between the level
    that keeps the high-demand path above the minimum and the maximum. It keeps the
    pumps that are running, adds the cheapest pumps (kWh per gallon) when short and
    drops the most expensive ones while it can still meet the target. Minimum up /
    down times, must-run and out-of-service pumps are respected; the starts limit
    is not.
    """
    c = MG_PER_GPM_HOUR
    must, off = pr.masks()
    lock_lo, lock_hi = initial_locks(pr)
    order = np.argsort(pr.kw / np.maximum(pr.flow_gpm, 1e-9))
    state = np.asarray(pr.on0, int).copy()
    run = np.asarray(pr.run_hours0, int).copy()
    v = pr.storage0_mg
    shift_hi, _ = pr.band_shifts()
    protect = pr.min_mg + shift_hi[-1]
    target = pr.max_mg if protect >= pr.max_mg else 0.5 * (protect + pr.max_mg)
    on = np.zeros((pr.P, pr.H), int)
    for t in range(pr.H):
        forced_on = ((state == 1) & (run < pr.min_up)) | must | (lock_lo[:, t] == 1)
        forced_off = off | ((state == 0) & (run < pr.min_down)) | (lock_hi[:, t] == 0)
        forced_on &= ~forced_off
        new = ((state == 1) | forced_on) & ~forced_off
        desired = max(0.0, pr.demand_p50[t] + (target - v) / (c * close_gap_hours))
        q = float(pr.flow_gpm[new].sum())
        if q < desired:
            for p in order:
                if q >= desired:
                    break
                if not new[p] and not forced_off[p]:
                    new[p] = True
                    q += pr.flow_gpm[p]
        else:
            for p in order[::-1]:
                if new[p] and not forced_on[p] and q - pr.flow_gpm[p] >= desired:
                    new[p] = False
                    q -= pr.flow_gpm[p]
        on[:, t] = new
        v += c * (q - pr.demand_p50[t])
        run = np.where(new == state, run + 1, 1)
        state = new.astype(int)
    return on


def solve(pr: ScheduleProblem, method: str = "milp") -> ScheduleResult:
    """Solve with the MILP and fall back to the rule if it fails; or run the rule directly."""
    t0 = time.perf_counter()
    message = ""
    if method == "milp":
        try:
            res = solve_milp(pr)
            if res is not None:
                return res
            message = "MILP found no solution within the time limit"
        except Exception as exc:  # noqa: BLE001 - any solver failure must still yield a plan
            message = f"MILP error: {exc}"
    on = rule_plan(pr)
    out = evaluate_schedule(pr, on)
    return ScheduleResult(status="rule" if method == "rule" else "fallback", on=on, storage=out["storage"],
                          cost=out["cost"], starts=out["starts"], solve_s=time.perf_counter() - t0,
                          message=message)
