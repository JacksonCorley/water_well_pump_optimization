"""The command line (`python -m wellopt ...`), run end to end on a copy of the synthetic site."""
from __future__ import annotations

import json

import pandas as pd

from tests import synthetic as syn
from wellopt.__main__ import main
from wellopt.live.store import PlanStore


def wellopt(site, *args) -> int:
    return main(["--config", str(site / "config" / "site.toml"), *args])


def test_audit(site, capsys):
    assert wellopt(site, "audit") == 0
    report = json.loads((site / "reports" / "data_audit.json").read_text(encoding="utf-8"))
    assert report["energy_check"]["verdict"].startswith("OK")
    assert (site / "reports" / "data_audit.md").exists()
    assert capsys.readouterr().out.strip()


def test_plan_is_saved_to_the_store(site, capsys):
    assert wellopt(site, "plan", "--forecaster", "naive", "--at", "2019-04-08 00:00") == 0
    plans = PlanStore(site / "var" / "test.sqlite").latest_plans()
    assert set(plans) == {"North", "South"}
    assert plans["North"].timeline.index[0] == pd.Timestamp("2019-04-08 00:00")
    assert "== North:" in capsys.readouterr().out


def test_plan_from_the_next_hour_with_a_storage_override(site):
    assert wellopt(site, "plan", "--forecaster", "naive", "--method", "rule", "--storage-mg", "0.5") == 0
    plan = PlanStore(site / "var" / "test.sqlite").latest_plans()["South"]
    assert plan.timeline.index[0] == syn.DATA_END + syn.H1
    assert plan.summary["storage_now_mg"] == 0.5
    assert plan.summary["storage_source"] == "entered by the user"
    assert plan.status == "rule"


def test_backtest(site):
    assert wellopt(site, "backtest", "--start", "2019-04-05", "--days", "1", "--sections", "North",
                   "--policies", "historical,legacy_hourly,rule_naive") == 0
    md = (site / "reports" / "backtest_summary.md").read_text(encoding="utf-8")
    assert md.startswith("# Policy backtest: 2019-04-05 to 2019-04-06")
    summary = pd.read_csv(site / "reports" / "backtest_summary.csv")
    assert list(summary["policy"]) == ["historical", "legacy_hourly", "rule_naive"]


def test_train(site):
    assert wellopt(site, "train", "--forecaster", "naive") == 0
    assert sorted(p.name for p in (site / "models").iterdir()) == ["forecaster_North_naive.joblib",
                                                                    "forecaster_South_naive.joblib"]


def test_live_replay_once(site):
    assert wellopt(site, "live", "--source", "csv", "--once", "--forecaster", "naive", "--method", "rule") == 0
    runs = PlanStore(site / "var" / "test.sqlite").recent_runs()
    assert list(runs["status"]) == ["ok"]
    assert runs["plan_time"].iloc[0] == str(syn.DATA_END - pd.Timedelta(days=7))
