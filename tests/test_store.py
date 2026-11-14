"""SQLite plan store: runs, plans, alerts, retention and the historian cache (wellopt.live.store)."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from tests import synthetic as syn
from wellopt.live.store import PlanStore

T = syn.PLAN_AT


@pytest.fixture
def store(tmp_path) -> PlanStore:
    return PlanStore(tmp_path / "var" / "store.sqlite")


def shifted(plan, days: float):
    """The same plan, moved `days` earlier (only its hours change)."""
    d = pd.Timedelta(days=days)
    return replace(plan, schedule=plan.schedule.set_axis(plan.schedule.index - d),
                   timeline=plan.timeline.set_axis(plan.timeline.index - d))


def test_runs_are_recorded_newest_first(store):
    a = store.start_run(T, "csv-replay")
    b = store.start_run(T + syn.H1, "csv-replay")
    store.finish_run(a, "ok", "fine", data_end=T - syn.H1, duration_s=1.5)
    runs = store.recent_runs()
    assert list(runs["id"]) == [b, a]
    first = runs.set_index("id").loc[a]
    assert (first["status"], first["message"], first["duration_s"]) == ("ok", "fine", 1.5)
    assert first["data_end"] == str(T - syn.H1)
    assert runs.set_index("id").loc[b, "status"] == "running"


def test_a_run_cut_off_by_its_process_is_marked_interrupted(store):
    dead = store.start_run(T, "csv-replay")
    with store._connect() as con:  # it started two hours ago and never finished
        two_hours_ago = (pd.Timestamp.now() - pd.Timedelta(hours=2)).isoformat(sep=" ", timespec="seconds")
        con.execute("UPDATE runs SET started_at=? WHERE id=?", (two_hours_ago, dead))
    busy = store.start_run(T + syn.H1, "csv-replay")  # still within the hour: left alone
    store.start_run(T + 2 * syn.H1, "cli")
    runs = store.recent_runs().set_index("id")
    assert runs.loc[dead, "status"] == "interrupted"
    assert runs.loc[dead, "message"] == "the planner stopped before this run finished"
    assert runs.loc[busy, "status"] == "running"


def test_plans_round_trip(store, north_plan):
    run_id = store.save_plans({"North": north_plan}, data_end=T - syn.H1)
    latest = store.latest_plans()
    assert list(latest) == ["North"]
    back = latest["North"]
    pd.testing.assert_frame_equal(back.schedule, north_plan.schedule, check_freq=False, check_dtype=False)
    assert back.notes == north_plan.notes
    assert back.summary["run_id"] == run_id and back.summary["plan_id"] == 1 and back.summary["created_at"]
    assert back.summary["total_usd"] == pytest.approx(north_plan.summary["total_usd"])
    # save_plans without a run id records a finished run for the plans.
    run = store.recent_runs().set_index("id").loc[run_id]
    assert run["status"] == "ok" and run["plan_time"] == str(T)
    hist = store.plan_history("North")
    assert len(hist) == 1
    assert hist.loc[0, "plan_start"] == str(T)
    assert hist.loc[0, "status"] == north_plan.status
    assert store.plan_history("South").empty


def test_latest_plan_is_the_newest_per_section(store, north_plan):
    store.save_plans({"North": shifted(north_plan, 1)})
    store.save_plans({"North": north_plan})
    assert store.latest_plans()["North"].timeline.index[0] == T
    assert list(store.plan_history()["id"]) == [2, 1]


def test_alerts_are_deduplicated_until_acknowledged(store):
    a = store.add_alert("North", "warning", "k1", "first", T)
    b = store.add_alert("North", "warning", "k1", "again", T + 2 * syn.H1)
    c = store.add_alert("South", "warning", "k1", "other section", T + 2 * syn.H1)
    d = store.add_alert("North", "warning", "k1", "after the window", T + 7 * syn.H1)
    assert a["new"] and not b["new"] and c["new"] and d["new"]
    assert b["id"] == a["id"]
    store.acknowledge(d["id"])
    e = store.add_alert("North", "warning", "k1", "after acknowledging", T + 8 * syn.H1)
    assert e["new"]
    assert len(store.recent_alerts()) == 4
    open_ = store.recent_alerts(include_acknowledged=False)
    assert sorted(open_["id"]) == sorted([a["id"], c["id"], e["id"]])
    # Site-wide alerts (no section) are de-duplicated the same way.
    x = store.add_alert(None, "alert", "runner_error", "failed", T)
    y = store.add_alert(None, "alert", "runner_error", "failed again", T + syn.H1)
    assert x["new"] and not y["new"]
    # dedupe_hours=0 always records.
    assert store.add_alert(None, "alert", "runner_error", "failed", T + syn.H1, dedupe_hours=0)["new"]


def test_prune_keeps_the_newest_plan_per_section(store, north_plan):
    now = T + pd.Timedelta(days=600)
    for days in (400, 200, 500):  # ids 1, 2, 3: the newest (id 3) is also the oldest plan
        store.save_plans({"North": shifted(north_plan, days - 600)})  # starts `days` before `now`
    assert list(store.plan_history()["plan_start"]) == [str(now - pd.Timedelta(days=d)) for d in (500, 200, 400)]
    out = store.prune(plans_before=now - pd.Timedelta(days=365))
    assert out == {"plans": 1, "runs": 1, "hourly": 0}
    assert sorted(store.plan_history()["id"]) == [2, 3]
    assert len(store.recent_runs()) == 2
    assert store.prune() == {"plans": 0, "runs": 0, "hourly": 0}


def test_hourly_cache(store):
    idx = pd.date_range("2019-04-01", periods=4, freq="h", name="time")
    df = pd.DataFrame({"PMP_1_FLOW": [1.0, np.nan, 3.0, 4.0], "PMP_1_PRESS": [60.0, 61.0, 62.0, 63.0]}, index=idx)
    assert store.last_hourly_time() is None
    assert store.load_hourly().empty
    assert store.upsert_hourly(df) == 4
    back = store.load_hourly()
    assert back.index.name == "time" and list(back.index) == list(idx)
    assert list(back.columns) == list(df.columns)
    np.testing.assert_array_equal(back.to_numpy(), df.to_numpy())  # the gap comes back as NaN
    store.upsert_hourly(df.iloc[[1]].fillna(2.0))  # a late value replaces the gap
    assert store.load_hourly().loc[idx[1], "PMP_1_FLOW"] == 2.0
    assert store.last_hourly_time() == idx[-1]
    window = store.load_hourly(idx[1], idx[3])  # [start, end)
    assert list(window.index) == list(idx[1:3])
    assert store.prune(hourly_before=idx[2]) == {"plans": 0, "runs": 0, "hourly": 2}
    assert list(store.load_hourly().index) == list(idx[2:])


def test_weather_round_trip(store):
    empty = store.load_weather()
    assert empty.empty and list(empty.columns) == ["temp_f", "precip_prob"]
    idx = pd.date_range("2019-04-08", periods=3, freq="h", name="time")
    store.save_weather(pd.DataFrame({"temp_f": [70.0, np.nan, 74.0], "precip_prob": [0.1, 0.2, None]}, index=idx))
    w = store.load_weather()
    assert list(w.index) == list(idx)
    assert w["temp_f"].iloc[0] == 70.0 and pd.isna(w["temp_f"].iloc[1])
    assert pd.isna(w["precip_prob"].iloc[2])
    assert w["fetched_at"].notna().all()
    assert len(store.load_weather(idx[1])) == 2
