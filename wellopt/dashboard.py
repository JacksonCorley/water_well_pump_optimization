"""Operator dashboard (Dash). It reads what the planner and the report commands wrote; it never talks to SCADA.

  Live plan    latest stored plan per section: status, alerts, notes, demand, storage and pump charts,
               and a what-if re-plan (storage level, pumps out of service) that is never saved
  Backtest     reports/backtest_* from `python -m wellopt backtest`
  Data audit   reports/data_audit.json from `python -m wellopt audit`

Run it with `python app.py` or `python -m wellopt.dashboard`; in Docker, gunicorn serves `app:server`.
The figure builders are plain functions (plan or report in, plotly figure out) so they can be tested
and reused without a running server.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, State, ctx as callback_ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from .config import REPO_ROOT, SECTIONS, SiteConfig, load_config
from .live.store import PlanStore
from .reporting import TABLE_COLUMNS, _formatted, key_findings, policy_label
from .scheduler import SectionPlan, replan
from .tariff import Tariff

H1 = pd.Timedelta(hours=1)
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Chart tokens. The categorical slots are the first three of the validated reference palette
# (safe for any pairing, in both modes); dark mode uses its own steps of the same hues.
THEMES = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
              "baseline": "#c3c2b7", "neutral": "#f0efec", "series": ("#2a78d6", "#eb6834", "#1baf7a")},
    "dark": {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
             "baseline": "#383835", "neutral": "#383835", "series": ("#3987e5", "#d95926", "#199e70")},
}
# Status colors are reserved for state and always appear with an icon and a text label.
STATUS = {"good": ("#0ca30c", "✓"), "warning": ("#fab219", "▲"), "serious": ("#ec835a", "◆"),
          "critical": ("#d03b3b", "✕")}
PLAN_STATUS = {
    "optimal": ("good", "Optimal"),
    "staged": ("good", "Solved"),            # relax-and-fix: every stage solved to the MIP gap
    "rule": ("good", "Rule-based plan"),
    "time_limit": ("warning", "Solver time limit"),
    "fallback": ("serious", "Fallback rule plan"),
}
ALERT_LEVEL = {"alert": ("critical", "Alert"), "warning": ("warning", "Warning"), "info": ("good", "Info")}
POLICY_ORDER = ("historical", "legacy_hourly", "rule_naive", "rule_gbm", "rule_perfect",
                "milp_naive", "milp_gbm", "milp_perfect")
# Chart names: the full names (reporting.POLICY_LABELS) crowd a half-width card; tables and hovers keep them.
SHORT_LABELS = {"historical": "Historical", "legacy_hourly": "Original method (bound)",
                "rule_naive": "Rule, naive", "rule_gbm": "Rule, GBM", "rule_perfect": "Rule, perfect (bound)",
                "milp_naive": "MILP, naive", "milp_gbm": "MILP, GBM", "milp_perfect": "MILP, perfect (bound)"}


def short_label(policy: str) -> str:
    return SHORT_LABELS.get(policy, policy_label(policy))
EMPTY_STORE = ("No plans yet. Make one from the end of the history with `python -m wellopt plan`, or replay the "
               "history as if live with `python -m wellopt live --source csv --steps 24`.")


# ----------------------------------------------------------------------------- figure helpers
def _theme(theme: str | None) -> str:
    return theme if theme in THEMES else "light"


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def section_color(section: str, theme: str = "light") -> str:
    """Sections keep their color everywhere: North slot 1, South slot 2."""
    series = THEMES[_theme(theme)]["series"]
    order = list(SECTIONS)
    i = order.index(section) if section in order else len(order)
    return series[i] if i < len(series) else THEMES[_theme(theme)]["muted"]


def policy_style(policy: str, theme: str = "light") -> dict:
    """Policy families share a hue (MILP blue, rule orange, original method aqua); the forecast is the
    line dash (GBM solid, naive dotted, perfect dashed), so at most three hues appear. In bar charts the
    hindsight bounds (perfect forecast, original method) are hatched and naive-forecast bars are dotted."""
    t = THEMES[_theme(theme)]
    if policy == "historical":
        return {"color": t["ink2"], "dash": "solid", "pattern": ""}
    family, _, variant = policy.partition("_")
    color = {"milp": t["series"][0], "rule": t["series"][1], "legacy": t["series"][2]}.get(family, t["muted"])
    return {"color": color, "dash": {"naive": "dot", "perfect": "dash"}.get(variant, "solid"),
            "pattern": {"naive": ".", "perfect": "/", "hourly": "/"}.get(variant, "")}


def base_layout(theme: str, *, height: int = 320, ytitle: str | None = None, xtitle: str | None = None,
                hovermode: str = "x unified", legend: bool = True) -> dict:
    t = THEMES[_theme(theme)]
    axis = dict(gridcolor=t["grid"], gridwidth=1, zeroline=False, showline=True, linecolor=t["baseline"],
                ticks="outside", ticklen=4, tickcolor=t["baseline"], tickfont=dict(color=t["muted"], size=12),
                automargin=True)
    title_font = dict(color=t["ink2"], size=12)
    return dict(
        height=height, margin=dict(l=8, r=16, t=12, b=8),
        paper_bgcolor=t["surface"], plot_bgcolor=t["surface"],
        font=dict(family=FONT, color=t["ink2"], size=13),
        xaxis={**axis, "showgrid": False, "showspikes": True, "spikemode": "across", "spikesnap": "cursor",
               "spikethickness": 1, "spikecolor": t["muted"], "spikedash": "solid",
               "title": dict(text=xtitle, font=title_font)},
        yaxis={**axis, "showgrid": True, "title": dict(text=ytitle, font=title_font)},
        showlegend=legend,
        legend=dict(orientation="h", x=0, xanchor="left", y=1.02, yanchor="bottom", bgcolor="rgba(0,0,0,0)",
                    font=dict(color=t["ink2"], size=12)),
        hovermode=hovermode,
        hoverlabel=dict(bgcolor=t["surface"], bordercolor=t["baseline"], font=dict(family=FONT, color=t["ink"], size=12)),
    )


def empty_figure(theme: str, message: str = "", height: int = 240) -> go.Figure:
    t = THEMES[_theme(theme)]
    fig = go.Figure(layout=base_layout(theme, height=height, legend=False))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    if message:
        fig.add_annotation(text=message, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper",
                           font=dict(color=t["muted"], size=13))
    return fig


def on_peak_periods(timeline: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """(start, end) of each run of hours priced above the cheapest hour in the plan."""
    price = timeline["price_usd_per_kwh"].to_numpy(float)
    if not np.isfinite(price).any() or np.nanmax(price) - np.nanmin(price) < 1e-9:
        return []
    peak = price > np.nanmin(price) + 1e-9
    runs, start = [], None
    for t, on in zip(timeline.index, peak):
        if on and start is None:
            start = t
        elif not on and start is not None:
            runs.append((start, t))
            start = None
    if start is not None:
        runs.append((start, timeline.index[-1] + H1))
    return runs


def _shade_on_peak(fig: go.Figure, timeline: pd.DataFrame, theme: str) -> None:
    t = THEMES[_theme(theme)]
    for k, (x0, x1) in enumerate(on_peak_periods(timeline)):
        fig.add_shape(type="rect", xref="x", yref="paper", x0=x0, x1=x1, y0=0, y1=1, layer="below", line_width=0,
                      fillcolor=_rgba(t["muted"], 0.13), name="On-peak price", legendgroup="on-peak",
                      showlegend=k == 0)


def _vline(fig: go.Figure, x, text: str, theme: str) -> None:
    """Dotted vertical reference line with a label (built by hand: add_vline mishandles dates)."""
    t = THEMES[_theme(theme)]
    fig.add_shape(type="line", xref="x", yref="paper", x0=x, x1=x, y0=0, y1=1, layer="above",
                  line=dict(color=t["muted"], width=1, dash="dot"))
    fig.add_annotation(x=x, y=1, xref="x", yref="paper", text=text, showarrow=False, xanchor="left", yanchor="top",
                       xshift=4, font=dict(color=t["ink2"], size=11))


def _hline(fig: go.Figure, y: float, text: str, theme: str) -> None:
    t = THEMES[_theme(theme)]
    fig.add_shape(type="line", xref="paper", yref="y", x0=0, x1=1, y0=y, y1=y, layer="below",
                  line=dict(color=t["muted"], width=1, dash="dash"))
    fig.add_annotation(x=1, y=y, xref="paper", yref="y", text=text, showarrow=False, xanchor="right",
                       yanchor="bottom", font=dict(color=t["ink2"], size=11))


# ----------------------------------------------------------------------------- live plan figures
def demand_figure(plan: SectionPlan, theme: str = "light") -> go.Figure:
    """Forecast demand (median and band) against the planned production, on-peak hours shaded."""
    t = THEMES[_theme(theme)]
    blue, orange = t["series"][0], t["series"][1]
    tl = plan.timeline
    band = str(plan.summary.get("band", "p10-p90")).upper().replace("-", "–")
    fig = go.Figure(layout=base_layout(theme, height=330, ytitle="gpm"))
    _shade_on_peak(fig, tl, theme)
    fig.add_trace(go.Scatter(x=tl.index, y=tl["demand_lo"], mode="lines", line=dict(width=0), hoverinfo="skip",
                             showlegend=False, legendgroup="band"))
    fig.add_trace(go.Scatter(x=tl.index, y=tl["demand_hi"], mode="lines", line=dict(width=0), fill="tonexty",
                             fillcolor=_rgba(orange, 0.10), name=f"Demand {band}", hoverinfo="skip",
                             legendgroup="band"))
    fig.add_trace(go.Scatter(
        x=tl.index, y=tl["demand_p50"], mode="lines", line=dict(color=orange, width=2), name="Demand forecast (P50)",
        customdata=np.c_[tl["demand_lo"], tl["demand_hi"]],
        hovertemplate=f"Demand %{{y:,.0f}} gpm ({band} %{{customdata[0]:,.0f}}–%{{customdata[1]:,.0f}})<extra></extra>"))
    fig.add_trace(go.Scatter(
        x=tl.index, y=tl["production_gpm"], mode="lines", line=dict(color=blue, width=2, shape="hv"),
        name="Planned production", customdata=tl["pumps_on"],
        hovertemplate="Production %{y:,.0f} gpm, %{customdata:.0f} pumps<extra></extra>"))
    fig.update_xaxes(hoverformat="%a %d %b %H:%M")
    fig.update_yaxes(rangemode="tozero")
    return fig


def storage_figure(plan: SectionPlan, theme: str = "light") -> go.Figure:
    """Expected storage and the range it could take if demand runs high or low, against the limits.

    Storage values are end-of-hour levels, so the line starts with the level now at the plan start. The
    high/low paths keep this schedule fixed for the whole horizon, so late in the plan they can run past
    an empty or full tank; they are drawn capped at those limits (re-planning acts long before)."""
    t = THEMES[_theme(theme)]
    blue = t["series"][0]
    tl, s = plan.timeline, plan.summary
    now = float(s["storage_now_mg"])
    cap = float(s.get("usable_mg") or np.inf)
    x = [tl.index[0], *(tl.index + H1)]
    p50 = np.clip(np.r_[now, tl["storage_p50_mg"].to_numpy(float)], 0, cap)
    low = np.clip(np.r_[now, tl["storage_high_demand_mg"].to_numpy(float)], 0, cap)   # high demand drains it
    high = np.clip(np.r_[now, tl["storage_low_demand_mg"].to_numpy(float)], 0, cap)
    fig = go.Figure(layout=base_layout(theme, height=330, ytitle="million gallons"))
    _shade_on_peak(fig, tl, theme)
    for y, label in ((s.get("max_mg"), "Overflow limit"), (s.get("min_mg"), "Reserve"), (s.get("floor_mg"), "Floor")):
        if y is not None:
            _hline(fig, float(y), f"{label} {float(y):.2f}", theme)
    r = int(s.get("risk_horizon_hours") or 0)
    if 0 < r < len(tl):
        _vline(fig, tl.index[0] + r * H1, f"{r} h risk window", theme)
    fig.add_trace(go.Scatter(x=x, y=low, mode="lines", line=dict(width=0), hoverinfo="skip", showlegend=False,
                             legendgroup="range"))
    fig.add_trace(go.Scatter(x=x, y=high, mode="lines", line=dict(width=0), fill="tonexty",
                             fillcolor=_rgba(blue, 0.10), name="If demand runs high / low", hoverinfo="skip",
                             legendgroup="range"))
    fig.add_trace(go.Scatter(
        x=x, y=p50, mode="lines", line=dict(color=blue, width=2), name="Expected storage", customdata=np.c_[low, high],
        hovertemplate="Storage %{y:.2f} MG (high demand %{customdata[0]:.2f}, low demand %{customdata[1]:.2f})"
                      "<extra></extra>"))
    top = cap if np.isfinite(cap) else np.nanmax([np.nanmax(high), float(s.get("max_mg") or 0)])
    fig.update_yaxes(range=[0, top * 1.08])
    fig.update_xaxes(hoverformat="%a %d %b %H:%M")
    return fig


def merit_order(plan: SectionPlan) -> list[str]:
    """The plan's pumps, cheapest energy per gallon first (pump number when the table is missing)."""
    pumps = list(plan.schedule.columns)
    tbl = plan.pump_table()
    if "kwh_per_kgal" in tbl:
        cost = pd.to_numeric(tbl["kwh_per_kgal"], errors="coerce").reindex(pumps)
        return list(cost.sort_values(na_position="last", kind="stable").index)
    return sorted(pumps, key=lambda p: (len(p), p))


def schedule_figure(plan: SectionPlan, theme: str = "light") -> go.Figure:
    """Pump x hour grid of the plan; pumps ordered cheapest first."""
    t = THEMES[_theme(theme)]
    blue = t["series"][0]
    if plan.schedule.empty:
        return empty_figure(theme, "No pumps are available in this plan")
    pumps = merit_order(plan)
    tl = plan.timeline
    z = plan.schedule[pumps].T.to_numpy(int)
    tbl = plan.pump_table()
    flow = pd.to_numeric(tbl.get("flow_gpm", pd.Series(dtype=float)), errors="coerce").reindex(pumps)
    kw = pd.to_numeric(tbl.get("kw_plan", pd.Series(dtype=float)), errors="coerce").reindex(pumps)
    price = tl["price_usd_per_kwh"].to_numpy(float)
    text = [[(f"Running, {flow[p]:,.0f} gpm, {kw[p]:,.0f} kW" if np.isfinite(flow[p]) else "Running")
             if z[i, j] else "Off" for j in range(z.shape[1])] for i, p in enumerate(pumps)]
    text = [[f"{cell} · ${price[j]:.3f}/kWh" for j, cell in enumerate(row)] for row in text]
    fig = go.Figure(layout=base_layout(theme, height=max(240, 24 * len(pumps) + 70), hovermode="closest"))
    fig.add_trace(go.Heatmap(
        z=z, x=tl.index, y=pumps, zmin=0, zmax=1, showscale=False, xgap=2, ygap=2,
        colorscale=[[0, t["neutral"]], [0.5, t["neutral"]], [0.5, blue], [1, blue]], customdata=text,
        hovertemplate="%{y} · %{x|%a %H:%M}<br>%{customdata}<extra></extra>"))
    for name, color in (("Running", blue), ("Off", t["neutral"])):
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=name, hoverinfo="skip",
                                 marker=dict(symbol="square", size=12, color=color, line=dict(color=t["baseline"], width=1))))
    fig.update_yaxes(autorange="reversed", showgrid=False, type="category")
    fig.update_xaxes(showspikes=False)
    return fig


def map_figure(plan: SectionPlan, theme: str = "light", pumps: pd.DataFrame | None = None,
               height: int = 340) -> go.Figure:
    """Wells by location, marked by their planned state in the first hour.

    A schematic: north is up, but east-west spacing is stretched to fill the card (the wells lie along a
    north-south line, so a true-aspect map is a thin strip). Only running wells are labelled; hover or the
    pump table identifies the rest."""
    t = THEMES[_theme(theme)]
    blue = t["series"][0]
    tbl = pumps if pumps is not None else plan.pump_table()
    if tbl.empty or not {"x", "y"} <= set(tbl.columns):
        return empty_figure(theme, "No well coordinates in this plan", height=height)
    first = plan.schedule.iloc[0] if len(plan.schedule) else pd.Series(dtype=int)
    state = pd.Series("Off", index=tbl.index)
    state[first.reindex(tbl.index).fillna(0).astype(int).eq(1).to_numpy()] = "Running"
    state[[p for p in tbl.index if p not in plan.schedule.columns]] = "Out of service"
    styles = {
        "Running": dict(symbol="circle", size=13, color=blue, line=dict(color=t["surface"], width=2)),
        "Off": dict(symbol="circle-open", size=12, color=t["ink2"], line=dict(width=2)),
        "Out of service": dict(symbol="x-thin-open", size=11, color=t["muted"], line=dict(width=2, color=t["muted"])),
    }
    fig = go.Figure(layout=base_layout(theme, height=height, hovermode="closest"))
    mid = float(tbl["x"].median())
    for name, marker in styles.items():
        sel = tbl[state == name]
        if sel.empty:
            continue
        hover = [f"{p} · {name.lower()} next hour<br>"
                 + (f"{r['flow_gpm']:,.0f} gpm, {r['kw_plan']:,.0f} kW, {r['kwh_per_kgal']:.2f} kWh/kgal"
                    if pd.notna(r.get("kwh_per_kgal")) else "no planning data")
                 for p, r in sel.iterrows()]
        fig.add_trace(go.Scatter(
            x=sel["x"], y=sel["y"], mode="markers+text" if name == "Running" else "markers", name=name,
            marker=marker, text=[str(p).replace("PMP_", "") for p in sel.index],
            textposition=["middle right" if x >= mid else "middle left" for x in sel["x"]],
            textfont=dict(color=t["ink2"], size=11), customdata=hover, hovertemplate="%{customdata}<extra></extra>"))
    lo, hi = float(tbl["x"].min()), float(tbl["x"].max())
    pad = 0.35 * ((hi - lo) or 1e-3)
    fig.update_xaxes(visible=False, showspikes=False, range=[lo - pad, hi + pad])
    fig.update_yaxes(visible=False)
    return fig


# ----------------------------------------------------------------------------- backtest figures
def _policy_rank(policy: str) -> int:
    return POLICY_ORDER.index(policy) if policy in POLICY_ORDER else len(POLICY_ORDER)


def savings_figure(summary: pd.DataFrame, section: str, theme: str = "light",
                   column: str = "savings_vs_historical_pct") -> go.Figure:
    """Bill saving against historical operation, one bar per policy."""
    t = THEMES[_theme(theme)]
    df = summary[(summary["section"] == section) & (summary["policy"] != "historical")]
    if df.empty or column not in df:
        return empty_figure(theme, "No policies to compare")
    df = df.iloc[np.argsort([_policy_rank(p) for p in df["policy"]], kind="stable")]
    styles = [policy_style(p, theme) for p in df["policy"]]
    values = df[column].to_numpy(float)
    fig = go.Figure(layout=base_layout(theme, height=70 + 46 * len(df), hovermode="closest", legend=False,
                                       xtitle="% saved vs historical operation"))
    fig.add_trace(go.Bar(
        x=values, y=[short_label(p) for p in df["policy"]], orientation="h", width=0.5, cliponaxis=False,
        marker=dict(color=[s["color"] for s in styles], cornerradius=4, line=dict(width=0),
                    pattern=dict(shape=[s["pattern"] for s in styles], fgcolor=t["surface"], size=7, solidity=0.35)),
        text=[f"{v:.1f}%" for v in values], textposition="outside", textfont=dict(color=t["ink2"], size=12),
        customdata=[policy_label(p) for p in df["policy"]],
        hovertemplate="%{customdata}<br>%{x:.1f}% vs historical<extra></extra>"))
    lo, hi = min(0.0, np.nanmin(values)), max(0.0, np.nanmax(values))
    pad = 0.22 * max(hi - lo, 1.0)  # room for the value labels outside the bar ends
    fig.update_yaxes(autorange="reversed", showgrid=False, ticks="")
    fig.update_xaxes(showgrid=True, zeroline=True, zerolinecolor=t["baseline"], zerolinewidth=1, ticksuffix="%",
                     showspikes=False, range=[lo - (pad if lo < 0 else 0.04 * pad), hi + pad])
    return fig


def backtest_lines_figure(hourly: pd.DataFrame, section: str, column: str, theme: str = "light", *,
                          cumulative: bool = False, ytitle: str = "", refs=(), fmt: str = ",.0f",
                          yrange=None) -> go.Figure:
    """One line per policy over the backtest window (running total when `cumulative`)."""
    df = hourly[hourly["section"] == section]
    if df.empty or column not in df:
        return empty_figure(theme, "No hourly results for this section")
    fig = go.Figure(layout=base_layout(theme, height=340, ytitle=ytitle))
    for y, label in refs:
        _hline(fig, float(y), label, theme)
    for policy in sorted(df["policy"].unique(), key=_policy_rank):
        d = df[df["policy"] == policy].sort_values("time")
        y = d[column].cumsum() if cumulative else d[column]
        st = policy_style(policy, theme)
        fig.add_trace(go.Scatter(x=d["time"], y=y, mode="lines", name=short_label(policy),
                                 line=dict(color=st["color"], dash=st["dash"], width=2),
                                 hovertemplate=f"%{{y:{fmt}}}<extra>{short_label(policy)}</extra>"))
    fig.update_xaxes(hoverformat="%a %d %b %H:%M")
    if yrange is not None:
        fig.update_yaxes(range=list(yrange))
    return fig


def daily_table(hourly: pd.DataFrame, section: str, column: str, how: str = "sum", fmt: str = "{:,.0f}") -> pd.DataFrame:
    """Table twin of a backtest line chart: one row per day, one column per policy."""
    df = hourly[hourly["section"] == section]
    if df.empty or column not in df:
        return pd.DataFrame()
    piv = df.assign(day=pd.to_datetime(df["time"]).dt.normalize()).pivot_table(
        index="day", columns="policy", values=column, aggfunc=how)
    piv = piv[sorted(piv.columns, key=_policy_rank)]
    out = piv.apply(lambda col: col.map(lambda v: "" if pd.isna(v) else fmt.format(v)))
    out.columns = [policy_label(p) for p in piv.columns]
    out.insert(0, "Day", [f"{d:%a %d %b}" for d in piv.index])
    return out.reset_index(drop=True)


# ----------------------------------------------------------------------------- audit figures
def energy_check_figure(audit: dict, theme: str = "light") -> go.Figure:
    """Physics-implied kW divided by recorded kW, per pump. Above 1 means the recorded kW is too low
    to lift that much water at the assumed efficiency."""
    t = THEMES[_theme(theme)]
    pumps = pd.DataFrame(audit.get("pumps", []))
    if pumps.empty or "physics_to_recorded" not in pumps:
        return empty_figure(theme, "No energy check in the audit")
    pumps = pumps.dropna(subset=["physics_to_recorded"]).sort_values("physics_to_recorded", ascending=False)
    fig = go.Figure(layout=base_layout(theme, height=90 + 20 * len(pumps), hovermode="closest",
                                       xtitle="physics-implied kW ÷ recorded kW"))
    for sec in [s for s in SECTIONS if s in set(pumps["section"])] + sorted(set(pumps["section"]) - set(SECTIONS)):
        d = pumps[pumps["section"] == sec]
        fig.add_trace(go.Bar(
            x=d["physics_to_recorded"], y=d["pump"], orientation="h", name=sec, width=0.6,
            marker=dict(color=section_color(sec, theme), cornerradius=4, line=dict(width=0)),
            customdata=np.c_[d["kw_recorded"], d["kw_physics"]],
            hovertemplate="%{y}: %{x:.1f}×<br>recorded %{customdata[0]:.1f} kW, physics %{customdata[1]:.1f} kW"
                          "<extra>" + sec + "</extra>"))
    fig.add_shape(type="line", xref="x", yref="paper", x0=1, x1=1, y0=0, y1=1,
                  line=dict(color=t["muted"], width=1, dash="dash"))
    fig.add_annotation(x=1, y=1, xref="x", yref="paper", text="1× = recorded matches physics", showarrow=False,
                       xanchor="left", yanchor="bottom", xshift=4, font=dict(color=t["ink2"], size=11))
    fig.update_layout(barmode="overlay")
    fig.update_yaxes(categoryorder="array", categoryarray=list(pumps["pump"]), autorange="reversed",
                     showgrid=False, ticks="")
    fig.update_xaxes(showgrid=True, showspikes=False, rangemode="tozero")
    return fig


def merit_figure(audit: dict, theme: str = "light", label_top: int = 2) -> go.Figure:
    """Energy per gallon against how often each pump ran. Expensive pumps that run a lot are where
    the savings are; the biggest energy users in each section are labeled."""
    t = THEMES[_theme(theme)]
    pumps = pd.DataFrame(audit.get("pumps", []))
    if pumps.empty or not {"kwh_per_kgal", "duty"} <= set(pumps.columns):
        return empty_figure(theme, "No pump table in the audit")
    pumps = pumps.dropna(subset=["kwh_per_kgal", "duty"])
    share = audit.get("energy_share", {})
    fig = go.Figure(layout=base_layout(theme, height=380, hovermode="closest",
                                       xtitle="kWh per 1,000 gal (recorded kW)", ytitle="% of hours running"))
    x_mid = float(pumps["kwh_per_kgal"].max()) / 2  # labels in the right half point left so they stay inside
    for sec in [s for s in SECTIONS if s in set(pumps["section"])] + sorted(set(pumps["section"]) - set(SECTIONS)):
        d = pumps[pumps["section"] == sec]
        top = sorted(share.get(sec, {}).items(), key=lambda kv: -kv[1])[:label_top]
        labels = {p: f"{p} ({v:.0%} of {sec} energy)" for p, v in top}
        fig.add_trace(go.Scatter(
            x=d["kwh_per_kgal"], y=100 * d["duty"], mode="markers+text", name=sec,
            marker=dict(size=11, color=section_color(sec, theme), line=dict(color=t["surface"], width=2)),
            text=[labels.get(p, "") for p in d["pump"]],
            textposition=["middle left" if x > x_mid else "middle right" for x in d["kwh_per_kgal"]],
            textfont=dict(color=t["ink2"], size=11),
            customdata=np.c_[d["pump"], d["flow_gpm"]],
            hovertemplate="%{customdata[0]}: %{x:.3f} kWh/kgal, ran %{y:.0f}% of hours, %{customdata[1]:,.0f} gpm"
                          "<extra>" + sec + "</extra>"))
    fig.update_xaxes(showgrid=True, showspikes=False, rangemode="tozero")
    fig.update_yaxes(rangemode="tozero", ticksuffix="%")
    return fig


# ----------------------------------------------------------------------------- HTML pieces
def badge(level: str, label: str) -> html.Span:
    color, icon = STATUS[level]
    return html.Span([html.Span(icon, className="wo-icon", style={"color": color}, **{"aria-hidden": "true"}), label],
                     className="wo-badge")


def pill(text: str) -> html.Span:
    return html.Span(text, className="wo-pill")


def tile(value: str, label: str, sub=None) -> html.Div:
    return html.Div([html.Div(value, className="wo-tile-value"), html.Div(label, className="wo-tile-label"),
                     html.Div(sub, className="wo-tile-sub") if sub else None], className="wo-tile")


def card(title: str | None, *children, subtitle: str | None = None, **kwargs) -> html.Section:
    head = [html.H2(title)] if title else []
    if subtitle:
        head.append(html.P(subtitle, className="wo-sub"))
    return html.Section([*head, *children], className="wo-card", **kwargs)


def html_table(df: pd.DataFrame, *, left=(), max_height: int | None = None) -> html.Div:
    """A plain table; `left` lists the text columns (numbers align right)."""
    if df is None or df.empty:
        return html.P("Nothing to show.", className="wo-sub")
    cols = [str(c) for c in df.columns]
    head = html.Thead(html.Tr([html.Th(c, className="l" if c in left else None) for c in cols]))
    body = html.Tbody([html.Tr([html.Td("" if v is None else str(v), className="l" if c in left else None)
                                for c, v in zip(cols, row)]) for row in df.itertuples(index=False)])
    style = {"maxHeight": f"{max_height}px"} if max_height else None
    return html.Div(html.Table([head, body], className="wo-table"), className="wo-table-wrap", style=style)


def table_view(table, label: str = "Table view") -> html.Details:
    return html.Details([html.Summary(label), table], className="wo-details")


def graph(id_: str, height: int = 320) -> dcc.Graph:
    return dcc.Graph(id=id_, figure=empty_figure("light", height=height), style=box(height),
                     # No mode bar: it covers the top legend on touch screens. Drag still zooms,
                     # double-click resets, and every chart has a table view.
                     config={"displaylogo": False, "responsive": True, "displayModeBar": False})


def box(fig_or_height) -> dict:
    """Style for a Graph. A responsive Dash Graph drops layout.height and fills its own box, so the box
    has to carry the figure's height; callbacks send this alongside each figure."""
    height = fig_or_height if isinstance(fig_or_height, (int, float)) else fig_or_height.layout.height
    return {"height": f"{int(height or 320)}px"}


def _age(minutes: float) -> str:
    if minutes < 90:
        return f"{minutes:.0f} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} h"
    return f"{minutes / 1440:.0f} days"


def note_item(note: str) -> html.Li:
    for prefix, level, label in (("ALERT:", "critical", "Alert"), ("Warning:", "warning", "Warning"),
                                 ("Storage is ASSUMED", "warning", "Assumption")):
        if note.startswith(prefix):
            text = note[len(prefix):].strip() if prefix.endswith(":") else note
            return html.Li([badge(level, label), " ", text])
    return html.Li(note)


def section_card(plan: SectionPlan, stale_after_minutes: float, now: datetime | None = None) -> html.Section:
    """Status and headline numbers for one section's stored plan."""
    s = plan.summary
    level, label = PLAN_STATUS.get(plan.status, ("critical", str(plan.status)))
    badges = [badge(level, label)]
    if not s.get("storage_measured"):
        badges.append(badge("warning", "Storage assumed"))
    created = pd.Timestamp(s["created_at"]) if s.get("created_at") else None
    if created is not None:
        age = (pd.Timestamp(now or datetime.now()) - created).total_seconds() / 60
        if age > stale_after_minutes:
            badges.append(badge("warning", f"Plan is {_age(age)} old"))
    low, reserve, floor = s["storage_low_point_mg"], s["min_mg"], s["floor_mg"]
    low_badge = (badge("good", "Above reserve") if low >= reserve - 1e-6 else
                 badge("warning", "Below reserve") if low >= floor - 1e-6 else badge("critical", "Below floor"))
    running = int(plan.timeline["pumps_on"].iloc[0]) if len(plan.timeline) else 0
    tiles = [
        tile(f"{s['storage_now_mg']:.2f} MG", "Storage now", s.get("storage_source")),
        tile(f"{low:.2f} MG", f"Lowest in the next {s['risk_horizon_hours']} h if demand runs high",
             html.Span([low_badge, f" reserve {reserve:.2f} MG"])),
        tile(f"${s['energy_usd']:,.0f}", f"Energy cost, next {s['hours']} h",
             f"{s['starts']} starts · {running} pumps on in the first hour"),
    ]
    start = pd.Timestamp(s["start"])
    meta = [f"Plan from {start:%a %d %b %Y %H:%M}"]
    if created is not None:
        meta.append(f"made {created:%Y-%m-%d %H:%M}")
    if s.get("forecaster"):
        meta.append(f"forecast: {s['forecaster']}")
    return card(plan.section, html.Div(badges, className="wo-badges"), html.Div(tiles, className="wo-tiles"),
                html.P(" · ".join(meta), className="wo-sub"))


def plan_table(plan: SectionPlan) -> pd.DataFrame:
    """Table twin of the live charts: one row per planned hour."""
    tl, sched = plan.timeline, plan.schedule
    running = [", ".join(str(p).replace("PMP_", "") for p in sched.columns if row[p] == 1) for _, row in sched.iterrows()]
    return pd.DataFrame({
        "Hour": [f"{t:%a %d %H:%M}" for t in tl.index],
        "Demand P50 gpm": [f"{v:,.0f}" for v in tl["demand_p50"]],
        "Demand band gpm": [f"{a:,.0f}–{b:,.0f}" for a, b in zip(tl["demand_lo"], tl["demand_hi"])],
        "Production gpm": [f"{v:,.0f}" for v in tl["production_gpm"]],
        "Storage at end MG": [f"{v:.2f}" for v in tl["storage_p50_mg"]],
        "Storage range MG": [f"{a:.2f}–{b:.2f}" for a, b in zip(tl["storage_high_demand_mg"], tl["storage_low_demand_mg"])],
        "Pumps on": [f"{v:.0f}" for v in tl["pumps_on"]],
        "Running (pump no.)": running,
        "kW": [f"{v:,.0f}" for v in tl["kw"]],
        "$/kWh": [f"{v:.3f}" for v in tl["price_usd_per_kwh"]],
        "Energy $": [f"{v:,.2f}" for v in tl["energy_usd"]],
    })


def pumps_table(plan: SectionPlan) -> pd.DataFrame:
    """Table twin of the well map."""
    tbl = plan.pump_table()
    if tbl.empty:
        return pd.DataFrame()
    sched = plan.schedule
    rows = []
    for p, r in tbl.iterrows():
        planned = p in sched.columns
        rows.append({
            "Pump": p,
            "First hour": ("running" if sched[p].iloc[0] == 1 else "off") if planned else "out of service",
            "Hours on (plan)": f"{int(sched[p].sum())}" if planned else "",
            "gpm": f"{r['flow_gpm']:,.0f}" if pd.notna(r.get("flow_gpm")) else "",
            "kW": f"{r['kw_plan']:,.1f}" if pd.notna(r.get("kw_plan")) else "",
            "kWh/kgal": f"{r['kwh_per_kgal']:.3f}" if pd.notna(r.get("kwh_per_kgal")) else "",
            "Group": r.get("group") or "",
        })
    order = {p: i for i, p in enumerate(merit_order(plan))}
    return pd.DataFrame(rows).sort_values("Pump", key=lambda s: s.map(lambda p: order.get(p, 999))).reset_index(drop=True)


def whatif_summary(stored: SectionPlan, whatif: SectionPlan) -> pd.DataFrame:
    a, b = stored.summary, whatif.summary
    r = a.get("risk_horizon_hours", 6)

    def row(label, key, fmt):
        return {"": label, "Stored plan": fmt.format(a.get(key, 0) or 0), "What-if": fmt.format(b.get(key, 0) or 0)}

    return pd.DataFrame([
        row(f"Energy cost, next {a.get('hours', 48)} h", "energy_usd", "${:,.0f}"),
        row("Demand-charge increase", "demand_usd", "${:,.0f}"),
        row("Pump starts", "starts", "{:.0f}"),
        row(f"Lowest storage in {r} h if demand runs high", "storage_low_point_mg", "{:.2f} MG"),
        row("Storage at the end (expected)", "storage_end_p50_mg", "{:.2f} MG"),
        row("Storage-limit penalty", "penalty_usd", "${:,.0f}"),
    ])


def _records(df: pd.DataFrame) -> list[dict]:
    return [] if df is None or df.empty else json.loads(df.to_json(orient="records", date_format="iso"))


def read_backtest(reports_dir: Path) -> dict | None:
    files = {"summary": "backtest_summary.csv", "hourly": "backtest_hourly.csv.gz", "meta": "backtest_meta.json"}
    paths = {k: Path(reports_dir) / f for k, f in files.items()}
    if not all(p.exists() for p in paths.values()):
        return None
    return {"summary": pd.read_csv(paths["summary"]),
            "hourly": pd.read_csv(paths["hourly"], parse_dates=["time"]),
            "meta": json.loads(paths["meta"].read_text(encoding="utf-8"))}


def read_audit(reports_dir: Path) -> dict | None:
    path = Path(reports_dir) / "data_audit.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ----------------------------------------------------------------------------- layout
def _section_options(sections) -> list[dict]:
    return [{"label": s, "value": s} for s in sections]


def build_layout(cfg: SiteConfig) -> html.Div:
    sections = list(cfg.storage) or list(SECTIONS)
    header = html.Header([
        html.Div([html.H1("Well pump scheduler"), html.Span(cfg.name, className="wo-sub")]),
        html.Div([pill("Advisory only: nothing is sent to SCADA"),
                  dbc.Select(id="theme-choice", size="sm", value="system", persistence=True, persistence_type="local",
                             options=[{"label": "System theme", "value": "system"},
                                      {"label": "Light", "value": "light"}, {"label": "Dark", "value": "dark"}],
                             className="wo-theme")], className="wo-header-right"),
    ], className="wo-header")

    whatif_controls = card(
        "What-if re-plan",
        html.P("Re-solve the stored plan with a different storage level or more pumps out of service. "
               "The result is shown here only; it is not saved and the runner does not see it.", className="wo-sub"),
        dbc.Row([
            dbc.Col([dbc.Label("Storage now (MG)", html_for="whatif-storage"),
                     dbc.Input(id="whatif-storage", type="number", min=0, step=0.01)], md=3),
            dbc.Col([dbc.Label("Also out of service", html_for="whatif-pumps"),
                     dcc.Dropdown(id="whatif-pumps", multi=True, options=[], placeholder="Pick pumps")], md=5),
            dbc.Col([dbc.Label("Method"),
                     dbc.RadioItems(id="whatif-method", value="milp", inline=True,
                                    options=[{"label": "MILP", "value": "milp"}, {"label": "Rule", "value": "rule"}])],
                    md=2),
            dbc.Col([dbc.Button("Run what-if", id="whatif-run", color="primary", n_clicks=0, className="me-2"),
                     dbc.Button("Clear", id="whatif-reset", color="secondary", outline=True, n_clicks=0)],
                    md=2, className="wo-buttons"),
        ], className="g-3"),
        html.Div(id="whatif-error", className="wo-error"),
    )

    live = html.Div([
        html.Div(card(None, dcc.Markdown(EMPTY_STORE)), id="live-empty", style={"display": "none"}),
        html.Div([
            html.Div(id="live-cards", className="wo-grid-2"),
            card("Alerts", dbc.Switch(id="show-acked", label="Show acknowledged", value=False),
                 html.Div(id="alerts-list")),
            html.Div([
                dbc.RadioItems(id="live-section", value=sections[0], inline=True, options=_section_options(sections),
                               className="wo-section-pick"),
                dbc.Button("What-if…", id="whatif-toggle", color="secondary", outline=True, size="sm", n_clicks=0),
            ], className="wo-toolbar"),
            dbc.Collapse(whatif_controls, id="whatif-collapse", is_open=False),
            dcc.Loading(html.Div(id="whatif-banner"), type="dot"),
            html.Div([card("Demand and planned production", graph("fig-demand", 330),
                           subtitle="Shaded columns are on-peak price hours."),
                      card("Storage", graph("fig-storage", 330),
                           subtitle="End-of-hour level. The band is the range if demand runs high or low, "
                                    "capped at an empty or full tank.")],
                     className="wo-grid-2"),
            html.Div([card("Pump schedule", graph("fig-schedule", 400), subtitle="Cheapest energy per gallon at the top."),
                      card("Wells, first planned hour", graph("fig-map", 400),
                           table_view(html.Div(id="live-pumps"), "Pump table"),
                           subtitle="Schematic, north up. Labels mark running wells.")],
                     className="wo-grid-2-1"),
            card("Plan notes", html.Ul(id="live-notes", className="wo-notes"),
                 table_view(html.Div(id="live-table"), "Hourly plan table (the charts above as a table)")),
            card("Recent planning runs", html.Div(id="live-runs")),
        ], id="live-main"),
    ])

    backtest = html.Div([
        html.Div(card(None, dcc.Markdown("No backtest yet. Run `python -m wellopt backtest` (about 30–60 minutes for "
                                         "a month; add `--jobs 4` to use more cores).")),
                 id="bt-empty", style={"display": "none"}),
        html.Div([
            card("What was compared", html.Div(id="bt-meta"), html.Div(id="bt-findings")),
            html.Div(dbc.RadioItems(id="bt-section", value=sections[0], inline=True,
                                    options=_section_options(sections), className="wo-section-pick"),
                     className="wo-toolbar"),
            html.Div([card("Bill saving vs historical operation", graph("fig-bt-savings", 420),
                           subtitle="Storage-adjusted, recorded kW. Hatched bars are bounds, not runnable policies."),
                      card("Running energy cost", graph("fig-bt-cost", 340), html.Div(id="bt-cost-table"),
                           subtitle="Energy charges only; demand charges are billed on the monthly peak.")],
                     className="wo-grid-2"),
            card("Simulated storage", graph("fig-bt-storage", 340), html.Div(id="bt-storage-table"),
                 subtitle="Historical operation keeps storage flat by construction (demand is taken as production). "
                          "The simulated tank cannot go past full or empty; surplus is spilled."),
            card("All policies", html.Div(id="bt-table")),
        ], id="bt-main"),
    ])

    audit = html.Div([
        html.Div(card(None, dcc.Markdown("No data audit yet. Run `python -m wellopt audit`.")),
                 id="audit-empty", style={"display": "none"}),
        html.Div([
            card("Energy data check", html.Div(id="audit-tiles", className="wo-tiles"), html.Div(id="audit-verdict")),
            html.Div([card("Recorded kW against physics, per pump", graph("fig-energy", 650),
                           subtitle="Above 1× the recorded kW is too low to lift that water at the assumed efficiency."),
                      card("Where the energy goes", graph("fig-merit", 380),
                           subtitle="Pumps to the right cost more per gallon; higher ones run more often.")],
                     className="wo-grid-2"),
            html.Div(id="audit-tables"),
        ], id="audit-main"),
    ])

    return html.Div([
        dcc.Store(id="theme", data="light"),
        dcc.Store(id="plans", data={}),
        dcc.Store(id="alerts", data=[]),
        dcc.Store(id="runs", data=[]),
        dcc.Store(id="whatif", data={}),
        dcc.Store(id="ack-done"),
        dcc.Interval(id="tick", interval=60_000),
        header,
        dbc.Tabs([dbc.Tab(live, label="Live plan", tab_id="live"),
                  dbc.Tab(backtest, label="Backtest", tab_id="backtest"),
                  dbc.Tab(audit, label="Data audit", tab_id="audit")],
                 id="tabs", active_tab="live", className="wo-tabs"),
    ], className="wo-page")


# ----------------------------------------------------------------------------- app
def create_app(config_path: str | Path | None = None, *, store_path: str | Path | None = None,
               reports_dir: str | Path | None = None) -> Dash:
    """Build the dashboard. Paths default to the [live] section of the site config."""
    cfg = load_config(config_path)
    tariff = Tariff(cfg.tariff)
    store_path = Path(store_path) if store_path else cfg.path(cfg.live.store_path)
    reports_dir = Path(reports_dir) if reports_dir else cfg.path(cfg.live.reports_dir)
    css = os.environ.get("WELLOPT_BOOTSTRAP_CSS", dbc.themes.BOOTSTRAP)
    app = Dash(__name__, title="Well pump scheduler", assets_folder=str(REPO_ROOT / "assets"),
               external_stylesheets=[] if css.lower() in ("", "none", "local") else [css])
    app.layout = build_layout(cfg)
    stores: dict[str, PlanStore] = {}

    def get_store() -> PlanStore | None:
        if not store_path.exists():
            return None
        if "store" not in stores:
            stores["store"] = PlanStore(store_path)
        return stores["store"]

    app.clientside_callback(
        """function(choice) {
            var dark = choice === 'dark' || (choice !== 'light' && window.matchMedia &&
                       window.matchMedia('(prefers-color-scheme: dark)').matches);
            var theme = dark ? 'dark' : 'light';
            document.documentElement.setAttribute('data-bs-theme', theme);
            document.documentElement.setAttribute('data-theme', theme);
            return theme;
        }""",
        Output("theme", "data"), Input("theme-choice", "value"))

    @app.callback(Output("plans", "data"), Output("alerts", "data"), Output("runs", "data"),
                  Input("tick", "n_intervals"), Input("ack-done", "data"))
    def load_store(_tick, _ack):
        store = get_store()
        if store is None:
            return {}, [], []
        plans = {sec: plan.to_record() for sec, plan in store.latest_plans().items()}
        return plans, _records(store.recent_alerts(limit=100)), _records(store.recent_runs(limit=12))

    @app.callback(
        Output("live-empty", "style"), Output("live-main", "style"), Output("live-cards", "children"),
        Output("fig-demand", "figure"), Output("fig-storage", "figure"), Output("fig-schedule", "figure"),
        Output("fig-map", "figure"), Output("live-notes", "children"), Output("live-table", "children"),
        Output("live-pumps", "children"), Output("whatif-banner", "children"), Output("whatif-pumps", "options"),
        Output("whatif-storage", "placeholder"),
        Output("fig-demand", "style"), Output("fig-storage", "style"), Output("fig-schedule", "style"),
        Output("fig-map", "style"),
        Input("plans", "data"), Input("whatif", "data"), Input("live-section", "value"), Input("theme", "data"))
    def render_live(plans, whatif, section, theme):
        theme = _theme(theme)
        if not plans:
            blank = empty_figure(theme)
            return ({}, {"display": "none"}, [], blank, blank, blank, blank, [], None, None, None, [], "",
                    *[box(blank)] * 4)
        stored = {sec: SectionPlan.from_record(rec) for sec, rec in plans.items()}
        order = [s for s in SECTIONS if s in stored] + sorted(set(stored) - set(SECTIONS))
        cards = [section_card(stored[s], cfg.live.stale_after_minutes) for s in order]
        section = section if section in stored else order[0]
        base = stored[section]
        shown, banner = base, None
        w = (whatif or {}).get(section)
        if w:
            if w.get("base_plan_id") == base.summary.get("plan_id"):
                shown = SectionPlan.from_record(w["record"])
                level, label = PLAN_STATUS.get(shown.status, ("critical", str(shown.status)))
                banner = card("What-if result",
                              html.Div([badge("warning", "What-if: not saved, not sent"), badge(level, label)],
                                       className="wo-badges"),
                              html.P(w.get("described", ""), className="wo-sub"),
                              html_table(whatif_summary(base, shown), left=("",)))
            else:
                banner = card(None, html.P("A newer plan arrived after this what-if was made, so the charts show "
                                           "the new plan. Run the what-if again to compare.", className="wo-sub"))
        tbl = base.pump_table()
        unavailable = set(base.inputs.get("state", {}).get("unavailable", []))
        options = [{"label": p, "value": p} for p in merit_order(base) if p not in unavailable]
        if tbl.empty:
            options = []
        notes = [note_item(n) for n in shown.notes] or [html.Li("No notes.")]
        sched = schedule_figure(shown, theme)
        figs = (demand_figure(shown, theme), storage_figure(shown, theme), sched,
                map_figure(shown, theme, tbl if not tbl.empty else None, height=int(sched.layout.height)))
        return ({"display": "none"}, {}, cards, *figs, notes,
                html_table(plan_table(shown), left=("Hour", "Running (pump no.)"), max_height=420),
                html_table(pumps_table(shown), left=("Pump", "First hour", "Group")), banner, options,
                f"{base.summary['storage_now_mg']:.2f} (stored plan)", *map(box, figs))

    @app.callback(Output("alerts-list", "children"), Output("live-runs", "children"),
                  Input("alerts", "data"), Input("runs", "data"), Input("show-acked", "value"))
    def render_alerts(alerts, runs, show_acked):
        rows = [a for a in (alerts or []) if show_acked or not a.get("acknowledged")]
        if rows:
            items = []
            for a in rows:
                level, label = ALERT_LEVEL.get(a.get("level"), ("warning", str(a.get("level", "")).title()))
                action = (html.Span("acknowledged", className="wo-sub") if a.get("acknowledged") else
                          dbc.Button("Acknowledge", id={"type": "ack", "index": int(a["id"])}, n_clicks=0, size="sm",
                                     color="secondary", outline=True))
                items.append(html.Li([badge(level, label), html.Span(a.get("message", ""), className="wo-alert-msg"),
                                      html.Span(str(a.get("plan_time") or a.get("created_at") or ""), className="wo-sub"),
                                      action], className="wo-alert"))
            alerts_view = html.Ul(items, className="wo-alerts")
        else:
            alerts_view = html.P("No open alerts.", className="wo-sub")
        if runs:
            df = pd.DataFrame(runs)
            cols = {"started_at": "Started", "plan_time": "Plan hour", "source": "Source", "status": "Status",
                    "duration_s": "Seconds", "message": "Message"}
            df = df[[c for c in cols if c in df]].rename(columns=cols)
            if "Seconds" in df:
                df["Seconds"] = [("" if pd.isna(v) else f"{v:.1f}") for v in df["Seconds"]]
            runs_view = html_table(df.fillna(""), left=("Started", "Plan hour", "Source", "Status", "Message"))
        else:
            runs_view = html.P("No runs recorded.", className="wo-sub")
        return alerts_view, runs_view

    @app.callback(Output("ack-done", "data"), Input({"type": "ack", "index": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def acknowledge(_clicks):
        trig = callback_ctx.triggered_id
        if not isinstance(trig, dict) or not any(t.get("value") for t in callback_ctx.triggered):
            return no_update
        store = get_store()
        if store is not None:
            store.acknowledge(int(trig["index"]))
        return time.time()

    @app.callback(Output("whatif-collapse", "is_open"), Input("whatif-toggle", "n_clicks"),
                  State("whatif-collapse", "is_open"), prevent_initial_call=True)
    def toggle_whatif(_n, is_open):
        return not is_open

    @app.callback(Output("whatif", "data"), Output("whatif-error", "children"),
                  Input("whatif-run", "n_clicks"), Input("whatif-reset", "n_clicks"),
                  State("plans", "data"), State("live-section", "value"), State("whatif-storage", "value"),
                  State("whatif-pumps", "value"), State("whatif-method", "value"), State("whatif", "data"),
                  prevent_initial_call=True)
    def run_whatif(_run, _reset, plans, section, storage, pumps, method, current):
        current = dict(current or {})
        if callback_ctx.triggered_id == "whatif-reset":
            current.pop(section, None)
            return current, ""
        if not plans or section not in plans:
            return no_update, html.Span([badge("critical", "No plan"), " There is no stored plan for this section."])
        base = SectionPlan.from_record(plans[section])
        allowed = set(base.pump_table().index)
        out = [p for p in (pumps or []) if p in allowed]
        try:
            storage = None if storage in (None, "") else float(storage)
            result = replan(base, cfg, tariff, storage_mg=storage, out_of_service=out, method=method or "milp")
        except Exception as exc:  # show the reason instead of a broken page
            return no_update, html.Span([badge("critical", "What-if failed"), f" {exc}"])
        changes = []
        if storage is not None:
            changes.append(f"storage now {storage:.2f} MG (stored plan: {base.summary['storage_now_mg']:.2f})")
        if out:
            changes.append("also out of service: " + ", ".join(out))
        changes.append(f"{'MILP' if (method or 'milp') == 'milp' else 'rule-based'} plan, "
                       f"solved in {result.summary['solve_s']:.1f} s")
        current[section] = {"base_plan_id": base.summary.get("plan_id"), "record": result.to_record(),
                            "described": "The charts show the what-if: " + "; ".join(changes) + "."}
        return current, ""

    @app.callback(
        Output("bt-empty", "style"), Output("bt-main", "style"), Output("bt-meta", "children"),
        Output("bt-findings", "children"), Output("fig-bt-savings", "figure"), Output("fig-bt-cost", "figure"),
        Output("bt-cost-table", "children"), Output("fig-bt-storage", "figure"), Output("bt-storage-table", "children"),
        Output("bt-table", "children"),
        Output("fig-bt-savings", "style"), Output("fig-bt-cost", "style"), Output("fig-bt-storage", "style"),
        Input("bt-section", "value"), Input("theme", "data"), Input("tabs", "active_tab"))
    def render_backtest(section, theme, tab):
        if tab != "backtest":  # charts drawn in a hidden tab come out at the wrong size
            raise PreventUpdate
        theme = _theme(theme)
        bt = read_backtest(reports_dir)
        if bt is None:
            blank = empty_figure(theme)
            return {}, {"display": "none"}, None, None, blank, blank, None, blank, None, None, *[box(blank)] * 3
        summary, hourly, meta = bt["summary"], bt["hourly"], bt["meta"]
        st = (meta.get("storage") or {}).get(section, {})
        start, end = pd.Timestamp(meta["start"]), pd.Timestamp(meta["end"])
        meta_view = html.P(
            f"{start:%d %b %Y} to {end:%d %b %Y}, re-planned every {meta.get('replan_every_hours')} h with a "
            f"{meta.get('horizon_hours')} h look-ahead, {meta.get('power_model', 'recorded')} kW. Each policy is "
            "simulated closed-loop against actual demand. Storage is simulated from an assumed tank size.",
            className="wo-sub")
        try:
            findings = dcc.Markdown("\n".join(key_findings(summary, meta)), className="wo-md")
        except Exception:  # an older report without every column
            findings = None
        refs = [(st[k], f"{label} {st[k]:.2f}") for k, label in
                (("max_mg", "Overflow limit"), ("min_mg", "Reserve"), ("floor_mg", "Floor")) if k in st]
        yrange = (0, 1.08 * st["usable_mg"]) if st.get("usable_mg") else None
        table = summary.copy()
        table = table.iloc[np.lexsort(([_policy_rank(p) for p in table["policy"]], table["section"].to_numpy()))]
        fmt = _formatted(table, TABLE_COLUMNS)
        fmt.insert(0, "section", table["section"].to_numpy())
        savings = savings_figure(summary, section, theme)
        cost = backtest_lines_figure(hourly, section, "energy_usd", theme, cumulative=True, ytitle="$", fmt="$,.0f")
        storage = backtest_lines_figure(hourly, section, "storage_mg", theme, ytitle="million gallons", refs=refs,
                                        fmt=".2f", yrange=yrange)
        return ({"display": "none"}, {}, meta_view, findings, savings, cost,
                table_view(html_table(daily_table(hourly, section, "energy_usd", "sum", "${:,.0f}"), left=("Day",),
                                      max_height=360), "Daily energy cost table"),
                storage,
                table_view(html_table(daily_table(hourly, section, "storage_mg", "min", "{:.2f}"), left=("Day",),
                                      max_height=360), "Daily minimum storage table"),
                html_table(fmt.reset_index(drop=True), left=("section", "policy")),
                box(savings), box(cost), box(storage))

    @app.callback(
        Output("audit-empty", "style"), Output("audit-main", "style"), Output("audit-tiles", "children"),
        Output("audit-verdict", "children"), Output("fig-energy", "figure"), Output("fig-merit", "figure"),
        Output("audit-tables", "children"), Output("fig-energy", "style"), Output("fig-merit", "style"),
        Input("theme", "data"), Input("tabs", "active_tab"))
    def render_audit(theme, tab):
        if tab != "audit":
            raise PreventUpdate
        theme = _theme(theme)
        a = read_audit(reports_dir)
        if a is None:
            blank = empty_figure(theme)
            return {}, {"display": "none"}, None, None, blank, blank, None, box(blank), box(blank)
        ec = a.get("energy_check", {})
        corr = ec.get("merit_order_rank_corr_recorded_vs_physics", {}) or {}
        tiles = [
            tile(f"{ec.get('physics_to_recorded_median', float('nan')):.1f}×",
                 "Physics-implied kW ÷ recorded kW (median pump)",
                 f"p10–p90 {ec.get('physics_to_recorded_p10', float('nan')):.1f}–"
                 f"{ec.get('physics_to_recorded_p90', float('nan')):.1f}×"),
            tile(f"{ec.get('pumps_above_100pct', 0)} of {ec.get('pumps_checked', 0)}",
                 "Pumps whose recorded kW implies over 100% efficiency"),
            tile(" · ".join(f"{s} {v:.2f}" for s, v in corr.items()) or "n/a",
                 "Merit-order agreement, recorded vs physics (rank correlation)"),
        ]
        verdict = ec.get("verdict", "")
        level = "critical" if verdict.startswith("FAIL") else "warning" if verdict.startswith("WARN") else "good"
        verdict_view = html.P([badge(level, verdict.split(":")[0] or "Check"), " ",
                               verdict.split(":", 1)[-1].strip()]) if verdict else None
        pumps = pd.DataFrame(a.get("pumps", []))
        pump_cols = {"pump": "Pump", "section": "Section", "group": "Group", "flow_gpm": "gpm",
                     "kw_recorded": "kW recorded", "kw_physics": "kW physics", "physics_to_recorded": "physics ÷ recorded",
                     "r2_kw_vs_flow_and_pressure": "R² kW ~ flow, pressure", "kwh_per_kgal": "kWh/kgal", "duty": "Running"}
        if not pumps.empty:
            p = pumps[[c for c in pump_cols if c in pumps]].copy()
            for c, f in (("flow_gpm", "{:,.0f}"), ("kw_recorded", "{:.1f}"), ("kw_physics", "{:.1f}"),
                         ("physics_to_recorded", "{:.2f}"), ("r2_kw_vs_flow_and_pressure", "{:.2f}"),
                         ("kwh_per_kgal", "{:.3f}"), ("duty", "{:.0%}")):
                if c in p:
                    p[c] = [("" if pd.isna(v) else f.format(v)) for v in p[c]]
            pump_view = html_table(p.rename(columns=pump_cols).fillna(""), left=("Pump", "Section", "Group"),
                                   max_height=420)
        else:
            pump_view = None
        qc = a.get("qc", {})
        qc_rows = [{"Section": s, **{k.replace("_", " "): v for k, v in vals.items()}}
                   for s, vals in (qc.get("sections") or {}).items()]
        issues = pd.DataFrame.from_dict(a.get("pump_issues", {}), orient="index")
        if not issues.empty:
            issues = issues.rename(columns=lambda c: c.replace("_", " ")).rename_axis("Pump").reset_index()
        gaps = qc.get("long_gaps", [])
        tables = html.Div([
            card("Per-pump energy table", pump_view, subtitle="The table view of both charts above."),
            html.Div([
                card("Data quality by section", html_table(pd.DataFrame(qc_rows), left=("Section",)),
                     html.P(f"{qc.get('hours', 0):,} hours, {qc.get('missing_rows', 0)} missing rows, "
                            f"{len(qc.get('dst_spring_gaps', []))} daylight-saving gaps"
                            + (f", long gaps: {'; '.join(gaps)}" if gaps else "") + ".", className="wo-sub")),
                card("Rejected pump readings", html_table(issues, left=("Pump",), max_height=320)),
            ], className="wo-grid-2"),
            card("Assumptions", html.Ul([html.Li(x) for x in a.get("assumptions", [])], className="wo-notes")),
        ])
        energy, merit = energy_check_figure(a, theme), merit_figure(a, theme)
        return {"display": "none"}, {}, tiles, verdict_view, energy, merit, tables, box(energy), box(merit)

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wellopt.dashboard", description="Run the operator dashboard.")
    ap.add_argument("--config", help="site config (default config/site.toml)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)
    create_app(args.config).run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
