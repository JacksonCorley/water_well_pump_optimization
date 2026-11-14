"""Data audit: coverage, rejected readings, the energy physics check and the merit order."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .config import SECTIONS, SiteConfig
from .data import QCResult
from .pumps import energy_sanity


def audit(qc: QCResult, table: pd.DataFrame, cfg: SiteConfig) -> dict:
    t = table[table["available"]].copy()
    t["physics_to_recorded"] = t["kw_physics"] / t["kw_recorded"]
    kwh_rec = t["kw_recorded"] / (t["flow_gpm"] * 0.06)
    kwh_phys = t["kw_physics"] / (t["flow_gpm"] * 0.06)
    spearman = {sec: float(kwh_rec[t["section"] == sec].corr(kwh_phys[t["section"] == sec], method="spearman"))
                for sec in SECTIONS if (t["section"] == sec).sum() > 2}
    on = qc.on.reindex(columns=t.index).fillna(0)
    energy_share = {}
    for sec in SECTIONS:
        cols = t.index[t["section"] == sec]
        e = (on[cols] * t.loc[cols, "kw_plan"]).sum()
        energy_share[sec] = (e / e.sum()).sort_values(ascending=False).round(4).to_dict() if e.sum() > 0 else {}
    ratio = t["physics_to_recorded"]
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "site": cfg.name,
        "data_start": str(qc.index.min()), "data_end": str(qc.index.max()),
        "qc": qc.summary,
        "pump_issues": qc.pump_issues[qc.pump_issues.sum(axis=1) > 0].astype(int).to_dict(orient="index"),
        "energy_check": energy_sanity(table) | {
            "physics_to_recorded_median": float(ratio.median()),
            "physics_to_recorded_p10": float(ratio.quantile(0.1)),
            "physics_to_recorded_p90": float(ratio.quantile(0.9)),
            "median_r2_kw_vs_flow_and_pressure": float(t["r2_kw_vs_flow_and_pressure"].median()),
            "merit_order_rank_corr_recorded_vs_physics": spearman,
            "assumed_wire_to_water_efficiency": cfg.wire_to_water_efficiency,
        },
        "pumps": t[["section", "group", "flow_gpm", "kw_recorded", "kw_physics", "press_psi", "lift_ft",
                    "implied_efficiency", "physics_to_recorded", "r2_kw_vs_flow", "r2_kw_vs_flow_and_pressure",
                    "kwh_per_kgal", "duty", "run_hours"]].round(4).reset_index().to_dict(orient="records"),
        "unavailable_pumps": table.index[~table["available"]].tolist(),
        "energy_share": energy_share,
        "assumptions": assumptions(cfg),
    }


def assumptions(cfg: SiteConfig) -> list[str]:
    """Settings that are placeholders until the utility confirms them."""
    t = cfg.tariff
    out = [f"{sec} usable storage {cfg.storage[sec].usable_mg:.2f} MG (min {cfg.storage[sec].min_frac:.0%}, "
           f"floor {cfg.storage[sec].floor_frac:.0%}, max {cfg.storage[sec].max_frac:.0%}). No tank telemetry "
           "in the export, so demand = production and storage is simulated." for sec in SECTIONS]
    out += [f"Tariff: {t.off_peak_usd_per_kwh:.3f} $/kWh off-peak, {t.on_peak_usd_per_kwh:.3f} $/kWh on-peak "
            f"{t.on_peak_start_hour:02d}:00-{t.on_peak_end_hour:02d}:00"
            f"{' weekdays' if t.on_peak_weekdays_only else ''}, demand charge "
            f"{t.demand_charge_usd_per_kw_month:.2f} $/kW-month, "
            f"{'one meter per pump' if not cfg.meters else f'{len(cfg.meters)} shared meters'}.",
            f"Pump rules: min up {cfg.ops.min_up_hours} h, min down {cfg.ops.min_down_hours} h, "
            f"max {cfg.ops.max_starts_per_day} starts per pump per day, ${cfg.ops.start_cost_usd:.0f} per start.",
            f"Power model for planning: {cfg.power_source} kW "
            f"(physics model assumes {cfg.wire_to_water_efficiency:.0%} wire-to-water efficiency).",
            f"Timestamps are local wall clock ({cfg.timezone}); the spring daylight-saving hour is missing.",
            f"Out of service: {', '.join(cfg.out_of_service) or 'none'}. Must run: {', '.join(cfg.must_run) or 'none'}."]
    return out


def md_table(df: pd.DataFrame, floatfmt: str = "{:.3f}") -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "---|" * len(cols)]
    for _, row in df.iterrows():
        cells = []
        for v in row:
            if isinstance(v, (float, np.floating)):
                cells.append("" if pd.isna(v) else floatfmt.format(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def to_markdown(a: dict) -> str:
    q, e = a["qc"], a["energy_check"]
    lines = [f"# Data audit: {a['site']}", "",
             f"Generated {a['generated']} from hourly data {a['data_start']} to {a['data_end']}.", "",
             "## 1. Energy data fails a physics check", "",
             f"**{e['verdict']}**", "",
             f"Lifting water takes at least flow x head / 3960 hp. Using the discharge pressure alone (no well "
             f"lift) and a perfect motor and pump, {e['pumps_above_100pct']} of {e['pumps_checked']} pumps record "
             f"*less* power than that minimum (median implied efficiency {e['median_implied_efficiency']:.0%}). "
             f"Real wire-to-water efficiency is 50-75%.", "",
             f"The recorded kW still tracks flow x pressure closely (median R² {e['median_r2_kw_vs_flow_and_pressure']:.2f}), "
             f"and the physics estimate is a fairly steady {e['physics_to_recorded_median']:.1f}x the recorded value "
             f"(10th-90th percentile {e['physics_to_recorded_p10']:.1f}x-{e['physics_to_recorded_p90']:.1f}x). "
             "That pattern points to a scaling or aggregation error in the kW tag, not random noise.", "",
             "Consequences:", "",
             "* Absolute kWh and $ figures from this data (including the original project's savings claims) "
             "cannot be trusted until the tag is reconciled with utility bills.",
             "* Rankings are more robust: the rank correlation of kWh/kgal under recorded vs physics power is "
             + ", ".join(f"{s} {v:.2f}" for s, v in e["merit_order_rank_corr_recorded_vs_physics"].items())
             + ". Savings measured *relative* to historical operation are therefore more meaningful than dollars.",
             "* Set `[power_model] source = \"physics\"` in `config/site.toml` to plan with the physics model instead.",
             "", "## 2. Coverage and rejected readings", "",
             f"* {q['hours']} hourly slots, {q['missing_rows']} missing from the export.",
             f"* Spring daylight-saving gaps: {', '.join(q['dst_spring_gaps']) or 'none'}.",
             f"* Gaps over 2 hours: {', '.join(q['long_gaps']) or 'none'}."]
    for sec, s in q["sections"].items():
        lines.append(f"* {sec}: {s['valid_hours']} valid production hours; {s['invalid_pump_reading']} hours with "
                     f"a rejected pump reading; {s['all_pumps_zero']} all-zero hours; {s['above_capacity']} above "
                     f"capacity; {s['imputed_for_models']} hours imputed for model inputs (never scored).")
    if a["pump_issues"]:
        lines += ["", "Rejected readings by pump (flow out of range, kW out of range, negative pressure while "
                  "running, flow without kW):", "",
                  md_table(pd.DataFrame(a["pump_issues"]).T.reset_index().rename(columns={"index": "pump"}), "{:.0f}")]
    pumps = pd.DataFrame(a["pumps"])
    lines += ["", "## 3. Pump table", "",
              "Planning flow and power are means over valid running hours. `implied_eff` uses discharge "
              "pressure only, so real efficiency would be even higher than shown: anything above 1.0 is impossible.", ""]
    for sec in SECTIONS:
        p = pumps[pumps["section"] == sec].sort_values("kwh_per_kgal")
        if p.empty:
            continue
        share = a["energy_share"].get(sec, {})
        p = p.assign(energy_share=[share.get(x, np.nan) for x in p["pump"]])
        lines += [f"### {sec} (cheapest first)", "",
                  md_table(p[["pump", "group", "flow_gpm", "kw_recorded", "kw_physics", "implied_efficiency",
                               "physics_to_recorded", "kwh_per_kgal", "duty", "energy_share"]]), ""]
        top = p.sort_values("energy_share", ascending=False).iloc[0]
        lines.append(f"Largest energy user: {top['pump']} ({top['energy_share']:.0%} of {sec} pump energy, "
                     f"{top['kwh_per_kgal']:.3f} kWh/kgal, runs {top['duty']:.0%} of hours).")
        lines.append("")
    lines += [f"Unavailable (no data or out of service): {', '.join(a['unavailable_pumps']) or 'none'}.", "",
              "## 4. Assumptions to confirm", ""] + [f"* {x}" for x in a["assumptions"]]
    return "\n".join(lines) + "\n"


def write_audit(a: dict, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "data_audit.json").write_text(json.dumps(a, indent=2, default=str))
    path = reports_dir / "data_audit.md"
    path.write_text(to_markdown(a), encoding="utf-8")
    return path
