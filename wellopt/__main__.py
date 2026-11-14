"""Command line interface: python -m wellopt <command> [options]

  audit          data quality + energy physics report        -> reports/data_audit.md
  forecast-eval  rolling-origin forecast backtest             -> reports/forecast_eval.*
  backtest       closed-loop comparison of operating policies -> reports/backtest_*.*
  train          fit forecasters on all history               -> models/
  plan           plan both sections as of a past hour         -> plan store (var/)
  live           advisory loop against a data source          -> plan store, alerts
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import pandas as pd

from .context import load_context


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _sections(arg: str) -> list[str]:
    return [s.strip() for s in arg.split(",") if s.strip()]


def cmd_audit(ctx, args) -> None:
    from .diagnostics import audit, write_audit
    path = write_audit(audit(ctx.qc, ctx.table, ctx.cfg), ctx.reports_dir())
    _log(f"wrote {path}")
    print(path.read_text(encoding="utf-8").split("## 2.")[0])


def cmd_forecast_eval(ctx, args) -> None:
    from .diagnostics import md_table
    from .forecast import QuantileGBM, SeasonalNaive, evaluate
    end = pd.Timestamp(args.end) if args.end else ctx.qc.index.max().normalize()
    start = pd.Timestamp(args.start) if args.start else end - pd.Timedelta(days=args.days)
    fc = ctx.cfg.forecast
    H = ctx.cfg.ops.horizon_hours
    makers = {"naive": lambda: SeasonalNaive(quantiles=fc.quantiles, horizon=H),
              "gbm": lambda: QuantileGBM(quantiles=fc.quantiles, horizon=H, origin_every=fc.origin_every_hours,
                                         max_iter=fc.max_iter, learning_rate=fc.learning_rate,
                                         calib_days=fc.calib_days)}
    summaries, details = [], []
    for sec in _sections(args.sections):
        factories = {name: makers[name] for name in _sections(args.models)}
        detail, summary = evaluate(ctx.qc.production[sec], factories, start, end, horizon=H,
                                   origin_every_hours=args.origin_every, refit_every_days=args.refit_days,
                                   quantiles=fc.quantiles, progress=lambda m, s=sec: _log(f"{s} {m}"))
        summaries.append(summary.assign(section=sec))
        details.append(detail.assign(section=sec))
    out = ctx.reports_dir()
    out.mkdir(parents=True, exist_ok=True)
    summary = pd.concat(summaries)[["section", "model", "horizon", "n", "MAE_gpm", "MAPE", "bias_gpm", "pinball",
                                    "coverage_p10_p90"]]
    summary.to_csv(out / "forecast_eval.csv", index=False)
    pd.concat(details).to_csv(out / "forecast_eval_detail.csv.gz", index=False)
    (out / "forecast_eval.json").write_text(json.dumps({"start": str(start), "end": str(end),
                                                        "origin_every_hours": args.origin_every}))
    md = [f"# Forecast backtest {start:%Y-%m-%d} to {end:%Y-%m-%d}", "",
          f"Rolling origin every {args.origin_every} h, models refit every {args.refit_days} days on data before "
          "each origin. Scored on real (not imputed) hours only. `vol_*` rows score the volume over the first 24 or "
          "48 hours; their coverage is the scenario band the scheduler uses.", ""]
    for sec in summary["section"].unique():
        md += [f"## {sec}", "", md_table(summary[summary["section"] == sec].drop(columns="section"), "{:.3f}"), ""]
    (out / "forecast_eval.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(summary.round(3).to_string(index=False))
    _log(f"wrote {out / 'forecast_eval.md'}")


def backtest_window(data_end: pd.Timestamp, horizon_hours: int, start=None, end=None, days=None):
    """Default: the last whole billing month that still leaves a full horizon of data after it,
    so demand charges are billed over a real month and perfect-forecast policies can look ahead."""
    last_ok = data_end - pd.Timedelta(hours=horizon_hours)
    if start and days and not end:
        start = pd.Timestamp(start)
        return start, min(start + pd.Timedelta(days=days), last_ok.normalize())
    if start or end or days:
        end = pd.Timestamp(end) if end else last_ok.normalize()
        start = pd.Timestamp(start) if start else end - pd.Timedelta(days=days or 30)
        return start, end
    end = last_ok.to_period("M").start_time  # the month containing last_ok is incomplete
    return (end - pd.Timedelta(days=1)).to_period("M").start_time, end


def cmd_backtest(ctx, args) -> None:
    from .backtest import run
    from .reporting import write_backtest
    start, end = backtest_window(ctx.qc.index.max(), ctx.cfg.ops.horizon_hours, args.start, args.end, args.days)
    _log(f"backtest {start} -> {end}, replan every {args.replan_every} h, {args.jobs} job(s)")
    table, results = run(_sections(args.sections), _sections(args.policies), start, end, qc=ctx.qc,
                         table=ctx.table, cfg=ctx.cfg, tariff=ctx.tariff, replan_every=args.replan_every,
                         progress=_log, jobs=args.jobs)
    path = write_backtest(table, results, ctx, start, end, args.replan_every)
    cols = ["section", "policy", "total_usd_storage_adjusted", "savings_vs_historical_pct",
            "energy_savings_vs_historical_pct", "kwh_savings_pct", "kwh_savings_physics_pct",
            "demand_usd", "on_peak_kwh_share", "starts", "starts_per_pump_day_max",
            "hours_below_min", "fallbacks", "time_limits", "solve_s_mean"]
    print(table[cols].round(3).to_string(index=False))
    _log(f"wrote {path}")


def cmd_train(ctx, args) -> None:
    from .live.models import train_and_save
    for sec in ctx.qc.production.columns:
        path = train_and_save(ctx, sec, args.forecaster)
        _log(f"saved {path}")


def cmd_plan(ctx, args) -> None:
    from .live.runner import make_plans
    from .live.store import PlanStore
    at = pd.Timestamp(args.at) if args.at else ctx.qc.index.max() + pd.Timedelta(hours=1)
    storage = {s: args.storage_mg for s in ctx.qc.production.columns} if args.storage_mg is not None else None
    store = PlanStore(ctx.store_path())
    plans = make_plans(ctx, at, forecaster_kind=args.forecaster, method=args.method, storage_mg=storage,
                       store=store, log=_log)
    store.save_plans(plans, data_end=at - pd.Timedelta(hours=1), source="cli")
    for sec, plan in plans.items():
        s = plan.summary
        print(f"\n== {sec}: {s['status']} in {s['solve_s']:.1f}s, ${s['energy_usd']:.0f} energy over "
              f"{s['hours']} h, {s['starts']} starts")
        for n in plan.notes:
            print(f"  - {n}")
    _log(f"saved to {ctx.store_path()}")


def cmd_live(args) -> None:
    from .live.runner import run_loop
    run_loop(args.config, args.history, source=args.source, once=args.once, interval_min=args.interval_min,
             replay_start=args.replay_start, steps=args.steps, forecaster_kind=args.forecaster, method=args.method,
             log=_log)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wellopt", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="site config (default config/site.toml)")
    ap.add_argument("--history", help="hourly SCADA CSV (default from config)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit", help="data quality and energy physics report")

    f = sub.add_parser("forecast-eval", help="rolling-origin forecast backtest")
    f.add_argument("--start")
    f.add_argument("--end")
    f.add_argument("--days", type=int, default=182, help="window length when --start is omitted")
    f.add_argument("--sections", default="North,South")
    f.add_argument("--models", default="naive,gbm")
    f.add_argument("--origin-every", type=int, default=24)
    f.add_argument("--refit-days", type=int, default=28)

    b = sub.add_parser("backtest", help="closed-loop policy comparison")
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--days", type=int, help="window length in days (default: the last whole billing month)")
    b.add_argument("--sections", default="North,South")
    b.add_argument("--policies", default="historical,legacy_hourly,rule_gbm,milp_gbm,milp_perfect")
    b.add_argument("--replan-every", type=int, default=6)
    b.add_argument("--jobs", type=int, default=1, help="run section/policy pairs in this many processes")

    t = sub.add_parser("train", help="fit forecasters on all history and save them")
    t.add_argument("--forecaster", default="gbm", choices=["gbm", "naive"])

    p = sub.add_parser("plan", help="plan both sections as of a past hour and store the plans")
    p.add_argument("--at", help="first hour to plan (default: the hour after the data ends)")
    p.add_argument("--forecaster", default="gbm", choices=["gbm", "naive"])
    p.add_argument("--method", default="milp", choices=["milp", "rule"])
    p.add_argument("--storage-mg", type=float, help="override the assumed current storage (MG, every section)")

    lv = sub.add_parser("live", help="advisory loop: ingest, QC, forecast, plan, alert")
    lv.add_argument("--source", default="csv", help="csv (replay the export) or pi (PI Web API)")
    lv.add_argument("--once", action="store_true", help="run one cycle and exit")
    lv.add_argument("--interval-min", type=int, default=60)
    lv.add_argument("--replay-start", help="csv source: first simulated 'now'")
    lv.add_argument("--steps", type=int, default=0, help="csv source: cycles to replay back to back (0 = forever)")
    lv.add_argument("--forecaster", default="gbm", choices=["gbm", "naive"])
    lv.add_argument("--method", default="milp", choices=["milp", "rule"])

    args = ap.parse_args(argv)
    if args.cmd == "live":
        cmd_live(args)
        return 0
    t0 = time.time()
    ctx = load_context(args.config, args.history)
    _log(f"loaded {len(ctx.qc.index)} hours in {time.time() - t0:.1f}s")
    {"audit": cmd_audit, "forecast-eval": cmd_forecast_eval, "backtest": cmd_backtest,
     "train": cmd_train, "plan": cmd_plan}[args.cmd](ctx, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
