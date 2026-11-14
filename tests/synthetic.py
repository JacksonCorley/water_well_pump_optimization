"""A small synthetic well field for the tests.

Six pumps in two sections, 100 days of hourly SCADA-style data with a daily
demand cycle, written in the same layout as the real site (config/site.toml,
a pump spec sheet and an hourly export). A few data problems are planted at
known hours so the quality checks can be tested exactly.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

H1 = pd.Timedelta(hours=1)
START = pd.Timestamp("2019-01-01 00:00")
DAYS = 100
DATA_END = START + pd.Timedelta(days=DAYS) - H1          # 2019-04-10 23:00
DST_GAP = pd.Timestamp("2019-03-10 02:00")               # missing, as in a real US export
PRESS_PSI = 60.0

# pump: (section, flow gpm, kW, x, y, group)
PUMPS = {
    "PMP_1": ("North", 1500.0, 110.0, 0.0, 2.0, "0"),
    "PMP_2": ("North", 1200.0, 120.0, 1.0, 2.5, "0"),
    "PMP_3": ("North", 1000.0, 70.0, 2.0, 2.0, "0"),
    "PMP_4": ("South", 1500.0, 160.0, 0.0, 0.0, "0"),
    "PMP_5": ("South", 1300.0, 90.0, 1.0, 0.5, "S2"),
    "PMP_6": ("South", 1100.0, 75.0, 2.0, 0.0, "S2"),
}

# Planted data problems, one per hour.
NEG_FLOW = START + pd.Timedelta(days=10, hours=5)    # PMP_2 reads -50 gpm
ABOVE_CAP = START + pd.Timedelta(days=12, hours=14)  # every North pump at 1.5x its flow
ALL_ZERO = START + pd.Timedelta(days=14, hours=3)    # every South pump reads 0
BAD_PRESS = START + pd.Timedelta(days=16, hours=10)  # PMP_5 running at -5 psi
NO_KW = START + pd.Timedelta(days=18, hours=11)      # PMP_6 running with 0 kW

PLAN_AT = pd.Timestamp("2019-04-08 00:00")           # first hour of the plans the tests share
BT_START, BT_END = pd.Timestamp("2019-04-05"), pd.Timestamp("2019-04-07")  # backtest window

SITE_TOML = """
[site]
name = "Synthetic test site"
timezone = "America/Chicago"
pump_specs = "specs.csv"
history_csv = "history.csv"

[power_model]
source = "recorded"
assumed_wire_to_water_efficiency = 0.70

[storage.North]
usable_mg = 0.6

[storage.South]
usable_mg = 0.6

[operations]
horizon_hours = 24
min_up_hours = 2
min_down_hours = 1
max_starts_per_day = 3
risk_horizon_hours = 6
solver_time_limit_s = 5
stage_hours = 6

[pumps]
out_of_service = []
must_run = []

[forecast]
quantiles = [0.1, 0.5, 0.9]
origin_every_hours = 7
max_iter = 30
learning_rate = 0.1
calib_days = 14

[live]
store_path = "var/test.sqlite"
models_dir = "models"
reports_dir = "reports"
lookback_days = 200
retrain_every_days = 28
stale_after_minutes = 120
plan_retention_days = 365
"""


def make_specs() -> pd.DataFrame:
    """Pump spec sheet with the real file's column names (including its spelling)."""
    return pd.DataFrame([{"Location": p, "Section": s, "X": x, "Y": y, "Group": g, "Static (ft)": 100.0,
                          "Specfic Capacity (gpm/ft)": 20.0, "Average_Flow(gpm)": q, "Average_Press(psi)": PRESS_PSI,
                          "Average_Power_Usage(kW-Hr)": kw}
                         for p, (s, q, kw, x, y, g) in PUMPS.items()])


def make_history(seed: int = 7) -> pd.DataFrame:
    """Hourly export. Each hour, operators add pumps (usually in merit order) until
    production covers that hour's demand, so production follows a daily cycle."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(START, periods=DAYS * 24, freq="h")
    n = len(idx)
    hour, dow = idx.hour.to_numpy(), idx.day_of_week.to_numpy()
    shape = 0.5 + 0.5 * np.sin(2 * np.pi * (hour - 9) / 24)
    cols = {}
    for sec in ("North", "South"):
        pumps = [p for p, v in PUMPS.items() if v[0] == sec]
        flow = np.array([PUMPS[p][1] for p in pumps])
        kw = np.array([PUMPS[p][2] for p in pumps])
        target = (flow.sum() * (0.30 + 0.30 * shape) * np.where(dow >= 5, 0.92, 1.0)
                  * (1 + 0.04 * rng.standard_normal(n)))
        merit = list(np.argsort(kw / flow))
        on = np.zeros((len(pumps), n), dtype=bool)
        for d in range(DAYS):
            order = merit if rng.random() < 0.6 else list(rng.permutation(len(pumps)))
            for t in range(d * 24, d * 24 + 24):
                q = 0.0
                for p in order:
                    if q >= target[t]:
                        break
                    on[p, t] = True
                    q += flow[p]
        for i, p in enumerate(pumps):
            run = on[i]
            cols[f"{p}_FLOW"] = np.where(run, flow[i] * (1 + 0.01 * rng.standard_normal(n)), 0.0)
            cols[f"{p}_PRESS"] = np.where(run, PRESS_PSI * (1 + 0.01 * rng.standard_normal(n)), -0.2)
            cols[f"{p}_PowerUsage(kW-H)"] = np.where(run, kw[i] * (1 + 0.01 * rng.standard_normal(n)), 0.0)
    df = pd.DataFrame(cols, index=idx)

    df.loc[NEG_FLOW, "PMP_2_FLOW"] = -50.0
    for p in ("PMP_1", "PMP_2", "PMP_3"):
        df.loc[ABOVE_CAP, [f"{p}_FLOW", f"{p}_PowerUsage(kW-H)", f"{p}_PRESS"]] = \
            [1.5 * PUMPS[p][1], PUMPS[p][2], PRESS_PSI]
    for p in ("PMP_4", "PMP_5", "PMP_6"):
        df.loc[ALL_ZERO, [f"{p}_FLOW", f"{p}_PowerUsage(kW-H)"]] = 0.0
    df.loc[BAD_PRESS, ["PMP_5_FLOW", "PMP_5_PowerUsage(kW-H)", "PMP_5_PRESS"]] = [1300.0, 90.0, -5.0]
    df.loc[NO_KW, ["PMP_6_FLOW", "PMP_6_PowerUsage(kW-H)", "PMP_6_PRESS"]] = [1100.0, 0.0, PRESS_PSI]
    df = df.drop(index=DST_GAP)
    df.index.name = "Time"
    return df


def write_site(root: Path) -> Path:
    """Write the site files under `root` and return the config path."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    cfg = root / "config" / "site.toml"
    cfg.write_text(SITE_TOML, encoding="utf-8")
    make_specs().to_csv(root / "specs.csv", index=False)
    make_history().to_csv(root / "history.csv")
    return cfg


def ctx_in(ctx, root: Path, **live):
    """A copy of `ctx` whose relative paths (store, models, reports) resolve under `root`,
    with any [live] settings in `live` replaced. Lets a test write without touching the
    shared session site."""
    return replace(ctx, cfg=replace(ctx.cfg, root=Path(root), live=replace(ctx.cfg.live, **live)))
