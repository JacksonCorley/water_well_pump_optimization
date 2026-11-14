"""Backtest reports: CSV and JSON for the app, Markdown for people."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .diagnostics import assumptions, md_table

POLICY_LABELS = {
    "historical": "Historical operation",
    "legacy_hourly": "Original method (hourly cheapest set, hindsight)",
    "rule_naive": "Storage-aware rule, naive forecast",
    "rule_gbm": "Storage-aware rule, GBM forecast",
    "rule_perfect": "Storage-aware rule, perfect forecast (bound)",
    "milp_naive": "MILP scheduler, naive forecast",
    "milp_gbm": "MILP scheduler, GBM forecast",
    "milp_perfect": "MILP scheduler, perfect forecast (bound)",
}
# Policies an operator could actually run: no hindsight, storage-aware.
REALISTIC = ("milp_gbm", "milp_naive", "rule_gbm", "rule_naive")

TABLE_COLUMNS = {  # column -> (header, format)
    "label": ("policy", "{}"),
    "total_usd_storage_adjusted": ("bill $ (adj.)", "{:,.0f}"),
    "savings_vs_historical_pct": ("bill saving %", "{:.1f}"),
    "energy_savings_vs_historical_pct": ("energy $ saving %", "{:.1f}"),
    "kwh_savings_pct": ("kWh saving %", "{:.1f}"),
    "kwh_savings_physics_pct": ("kWh saving % (physics)", "{:.1f}"),
    "demand_usd": ("demand charge $", "{:,.0f}"),
    "on_peak_kwh_share": ("on-peak kWh", "{:.0%}"),
    "starts": ("starts", "{:.0f}"),
    "starts_per_pump_day_max": ("max starts/pump/day", "{:.0f}"),
    "storage_min_mg": ("min storage MG", "{:.2f}"),
    "hours_below_min": ("h below reserve", "{:.0f}"),
    "spilled_mg": ("spilled MG", "{:.1f}"),
    "unserved_mg": ("unserved MG", "{:.2f}"),
    "fallbacks": ("fallbacks", "{:.0f}"),
    "solve_s_mean": ("mean solve s", "{:.1f}"),
}


def policy_label(policy: str) -> str:
    return POLICY_LABELS.get(policy, policy)


def _formatted(df: pd.DataFrame, columns: dict) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for col, (header, fmt) in columns.items():
        if col not in df:
            continue
        out[header] = ["" if (v is None or (isinstance(v, float) and np.isnan(v))) else fmt.format(v)
                       for v in df[col]]
    return out


def write_backtest(table: pd.DataFrame, results: dict, ctx, start, end, replan_every: int) -> Path:
    """Write backtest_summary.{csv,md}, backtest_meta.json, backtest_hourly.csv.gz and backtest_on.csv.gz."""
    out = ctx.reports_dir()
    out.mkdir(parents=True, exist_ok=True)
    cfg = ctx.cfg
    table = table.copy()
    table.insert(2, "label", [policy_label(p) for p in table["policy"]])
    table.to_csv(out / "backtest_summary.csv", index=False)

    hourly = pd.concat([r.hourly.assign(section=s, policy=p) for (s, p), r in results.items()])
    hourly.rename_axis("time").reset_index().to_csv(out / "backtest_hourly.csv.gz", index=False)
    on = pd.concat([r.on.rename_axis("time").reset_index().melt(id_vars="time", var_name="pump", value_name="on")
                    .assign(section=s, policy=p) for (s, p), r in results.items()])
    on.to_csv(out / "backtest_on.csv.gz", index=False)

    policies = list(dict.fromkeys(table["policy"]))
    sections = list(dict.fromkeys(table["section"]))
    meta = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "start": str(pd.Timestamp(start)), "end": str(pd.Timestamp(end)),
        "replan_every_hours": int(replan_every), "horizon_hours": cfg.ops.horizon_hours,
        "policies": policies, "sections": sections, "labels": {p: policy_label(p) for p in policies},
        "power_model": cfg.power_source,
        "storage": {s: {"usable_mg": cfg.storage[s].usable_mg, "min_mg": cfg.storage[s].min_mg,
                        "floor_mg": cfg.storage[s].floor_mg, "max_mg": cfg.storage[s].max_mg,
                        "initial_mg": cfg.storage[s].initial_mg} for s in sections},
        "assumptions": assumptions(cfg),
    }
    (out / "backtest_meta.json").write_text(json.dumps(meta, indent=2))
    path = out / "backtest_summary.md"
    path.write_text(to_markdown(table, meta), encoding="utf-8")
    return path


def _signed(pct: float, down: str, up: str) -> tuple[str, str]:
    """(word, magnitude) so a negative saving reads '1.3% higher', not '-1.3% lower'."""
    return (down if pct >= 0 else up), f"{abs(pct):.1f}%"


def _energy_use(recorded: float, physics: float) -> str:
    w1, v1 = _signed(recorded, "falls", "rises")
    w2, v2 = _signed(physics, "falls", "rises")
    return (f"Energy use {w1} {v1} with recorded kW and {v2 if w2 == w1 else f'{w2} {v2}'} "
            "with the physics power model.")


def key_findings(table: pd.DataFrame, meta: dict) -> list[str]:
    lines = []
    for sec in meta["sections"]:
        t = table[table["section"] == sec].set_index("policy")
        real = t[t.index.isin(REALISTIC)]
        if real.empty or "historical" not in t.index:
            continue
        if "unserved_mg" in real:
            failed = real[real["unserved_mg"] > 1e-6]
            for p, r in failed.iterrows():
                lines.append(f"* **{sec}**: {policy_label(p)} left {r.unserved_mg:.2f} MG of demand unserved "
                             f"({r.hours_unserved:.0f} h with an empty tank), so it is not counted as runnable.")
            real = real.drop(failed.index)
            if real.empty:
                continue
        b = real.sort_values("total_usd_storage_adjusted").iloc[0]
        h = t.loc["historical"]
        st = meta["storage"][sec]
        below = float(b.get("hours_below_min", 0) or 0)
        if below > 0:
            side = "above" if b.storage_min_mg >= st["floor_mg"] - 1e-9 else "below"
            low = (f"Simulated storage was under the {st['min_mg']:.2f} MG reserve for {below:.0f} h, bottoming at "
                   f"{b.storage_min_mg:.2f} MG ({side} the {st['floor_mg']:.2f} MG floor; assumed tank "
                   f"{st['usable_mg']:.1f} MG)")
        else:
            low = (f"Simulated storage stayed above the {st['min_mg']:.2f} MG reserve (lowest {b.storage_min_mg:.2f} MG; "
                   f"assumed tank {st['usable_mg']:.1f} MG)")
        bill_w, bill_v = _signed(b.savings_vs_historical_pct, "cuts", "raises")
        energy_w, energy_v = _signed(b.energy_savings_vs_historical_pct, "lower", "higher")
        lines.append(
            f"* **{sec}**: the best runnable policy, {policy_label(b.name)}, {bill_w} the bill by {bill_v} against "
            f"historical operation (storage-adjusted, recorded kW): energy cost {energy_v} {energy_w}, demand "
            f"charges ${b.demand_usd:,.0f} vs ${h.demand_usd:,.0f}. "
            f"{_energy_use(b.kwh_savings_pct, b.kwh_savings_physics_pct)} It made {b.starts:.0f} pump starts vs "
            f"{h.starts:.0f} historically. {low}"
            + (f"; the tank was full and spilling for {b.hours_spilling:.0f} h ({b.spilled_mg:.1f} MG)."
               if b.get("spilled_mg", 0) > 0.05 else "."))
        for bound in ("milp_perfect",):
            if bound in t.index and b.name.startswith("milp"):
                gap = t.loc[bound, "savings_vs_historical_pct"] - b.savings_vs_historical_pct
                lines.append(f"  A perfect demand forecast would add {gap:.1f} more points of savings." if gap >= 0
                             else f"  The perfect-forecast run saved {-gap:.1f} points less than this one, so "
                                  "the bound is loose here (solver time limits).")
        if "legacy_hourly" in t.index:
            lg = t.loc["legacy_hourly"]
            lines.append(f"  The original method (with hindsight) shows {lg.savings_vs_historical_pct:.1f}% but needs "
                         f"{lg.starts:.0f} starts, up to {lg.starts_per_pump_day_max:.0f} per pump per day.")
    return lines


def to_markdown(table: pd.DataFrame, meta: dict) -> str:
    r = meta["replan_every_hours"]
    lines = [f"# Policy backtest: {meta['start'][:10]} to {meta['end'][:10]}", "",
             f"Generated {meta['generated']}. Closed loop: every {r} h each storage-aware policy plans "
             f"{meta['horizon_hours']} h ahead using only data it would have had, the first {r} h of the plan are "
             "applied, storage moves with the *actual* demand, and the plan is redone.", "",
             "## Key findings", ""] + key_findings(table, meta) + [""]
    for sec in meta["sections"]:
        t = table[table["section"] == sec]
        lines += [f"## {sec}", "", md_table(_formatted(t, TABLE_COLUMNS)), ""]
        h = t[t["policy"] == "historical"]
        if not h.empty and h["demand_mg"].iloc[0] > 0:
            ratio = h["pumped_mg_at_plan_flows"].iloc[0] / h["demand_mg"].iloc[0]
            lines += [f"Planning-flow check: the historical on/off states at the planning flows deliver {ratio:.1%} "
                      "of the production actually recorded in this window"
                      + (" (close enough for a fair comparison)." if abs(ratio - 1) < 0.05 else
                         ". The gap is large: pump flows in this window differ from their long-run means, so "
                         "compare policies with care."), ""]
    storage = ", ".join(f"{s} {v['usable_mg']:.1f} MG" for s, v in meta["storage"].items())
    lines += [
        "## How to read this", "",
        "* **Dollars are relative.** Costs use the recorded kW, which fails the physics check (see "
        "`data_audit.md`). Percent savings mean more than dollar amounts. The physics column re-costs the same "
        "on/off schedules with the physics power model; when both columns agree, the ranking does not depend on "
        "the kW problem.",
        "* **Storage-adjusted.** A policy that ends the window with less water in storage is charged for refilling "
        "it at its own average cost per MG, and credited if it ends with more, so no policy wins by draining the "
        "tank.",
        "* **The simulated tank is physical.** It cannot rise above the usable volume or fall below empty. Water "
        "pumped into a full tank is spilled and its energy still counts; demand an empty tank cannot meet is "
        "unserved, charged at the policy's average cost per MG, and rules the policy out of the key findings.",
        "* **Historical and the original method have flat storage.** The export has no tank levels, so demand is "
        f"taken to be historical production. Only the storage-aware policies use storage ({storage}, ASSUMED). "
        "Their savings come from moving pumping in time and grow with the tank, so rerun this with the real "
        "usable volumes.",
        "* **The original method is a hindsight bound, not a policy.** It picks the cheapest pump set for each "
        "hour's actual production, ignores storage, start limits and minimum run times, and spills any surplus. "
        "Its start count shows why it could not be run as is.",
        "* **Perfect-forecast rows are bounds.** The gap between the GBM and perfect-forecast rows is the most a "
        "better demand forecast could add.",
        "* **Demand charges dominate short windows.** Each month's charge counts only the amount by which a "
        "policy raises that month's peak above the peak already set before the window started, so a window that "
        "starts on the 1st carries the whole month's charge. The default window is one whole billing month for "
        "that reason; the energy and kWh columns are free of this effect.",
        "* Hours with an unknown pump state count as off in the historical row, which slightly understates "
        "historical cost (conservative).", "",
        "## Assumptions to confirm", ""] + [f"* {a}" for a in meta["assumptions"]]
    return "\n".join(lines) + "\n"
