"""The live advisory cycle: data checks, where storage comes from, fallbacks, alerts and the
replay loop (wellopt.live.runner). Webhook posts are captured; nothing leaves the machine."""
from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest
import requests

from tests import synthetic as syn
from wellopt.context import load_context
from wellopt.live import runner
from wellopt.live.runner import (current_storage, data_freshness, forecast_section, plan_alerts, prune_store,
                                 recent_pump_issues, run_loop, run_once, send_webhook)
from wellopt.live.store import PlanStore

AFTER_DATA = syn.DATA_END + syn.H1  # 2019-04-11 00:00, the first hour with no data
CONFIGURED = "configured starting level (60% of 0.6 MG)"
WEBHOOK = "https://hooks.example.invalid/wellopt"


class _Ok:
    def raise_for_status(self):
        return None


@pytest.fixture
def posts(monkeypatch) -> list[dict]:
    """Capture webhook posts instead of sending them."""
    sent: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        sent.append({"url": url, "json": json, "timeout": timeout})
        return _Ok()

    monkeypatch.setattr(requests, "post", fake_post)
    return sent


def with_tank(ctx, last_reading=syn.DATA_END):
    raw = syn.make_history()
    raw["TNK_North_MG"] = 0.45
    raw.loc[raw.index > last_reading, "TNK_North_MG"] = float("nan")
    return load_context(cfg=ctx.cfg, raw=raw)


# ----------------------------------------------------------------------------- checks
def test_data_freshness(ctx):
    assert data_freshness(ctx, AFTER_DATA) == {"North": (syn.DATA_END, 0.0), "South": (syn.DATA_END, 0.0)}
    assert data_freshness(ctx, AFTER_DATA + 10 * syn.H1)["South"] == (syn.DATA_END, 10.0)


def test_recent_pump_issues(ctx):
    assert recent_pump_issues(ctx, AFTER_DATA) == {"North": [], "South": []}
    raw = syn.make_history()
    raw.loc[raw.index > syn.DATA_END - 4 * syn.H1, "PMP_2_FLOW"] = float("nan")  # last 4 hours missing
    assert recent_pump_issues(load_context(cfg=ctx.cfg, raw=raw), AFTER_DATA) == {"North": ["PMP_2"], "South": []}


def test_storage_from_the_user_or_the_configured_level(ctx):
    assert current_storage(ctx, "North", AFTER_DATA, override=0.42) == (0.42, False, "entered by the user")
    mg, measured, source = current_storage(ctx, "North", AFTER_DATA)
    assert (mg, measured, source) == (pytest.approx(0.36), False, CONFIGURED)


def test_storage_from_the_previous_plan(ctx, north_plan):
    tl = north_plan.timeline
    mg, measured, source = current_storage(ctx, "North", syn.PLAN_AT, north_plan)
    assert mg == pytest.approx(north_plan.summary["storage_now_mg"])
    assert (measured, source) == (False, "same as the previous plan for this hour")
    mg, _, source = current_storage(ctx, "North", syn.PLAN_AT + 3 * syn.H1, north_plan)
    assert mg == pytest.approx(tl.loc[syn.PLAN_AT + 2 * syn.H1, "storage_p50_mg"])
    assert source == "carried forward from the plan made for Mon 00:00"
    # More than 6 h on, the plan's forecast path is no longer trusted.
    assert current_storage(ctx, "North", syn.PLAN_AT + 7 * syn.H1, north_plan)[2] == CONFIGURED


def test_storage_from_tank_telemetry_while_it_is_fresh(ctx, north_plan):
    tank = with_tank(ctx)
    assert current_storage(tank, "North", AFTER_DATA) == (0.45, True, "tank telemetry at 2019-04-10 23:00")
    assert current_storage(tank, "South", AFTER_DATA)[2] == CONFIGURED  # no South tank tag
    # A measurement beats the previous plan's estimate...
    assert current_storage(tank, "North", syn.PLAN_AT, north_plan) == (0.45, True, "tank telemetry at 2019-04-07 23:00")
    # ...but a reading 3 hours old is too old to use.
    late = with_tank(ctx, last_reading=syn.DATA_END - 3 * syn.H1)
    assert current_storage(late, "North", AFTER_DATA) == (pytest.approx(0.36), False, CONFIGURED)


# ----------------------------------------------------------------------------- forecasting
def test_forecast_falls_back_to_naive_when_the_model_fails(ctx, monkeypatch):
    def no_model(*_a, **_k):
        raise RuntimeError("no model")

    monkeypatch.setattr(runner, "get_forecaster", no_model)
    fc, described, note = forecast_section(ctx, "North", AFTER_DATA, "gbm")
    assert described == "naive (fallback)"
    assert note == "The gbm demand forecast failed (no model); this plan uses the seasonal-naive forecast."
    assert len(fc) == ctx.cfg.ops.horizon_hours and fc.index[0] == AFTER_DATA


def test_forecast_falls_back_when_the_model_returns_nonsense(ctx, monkeypatch):
    class Negative:
        def predict(self, _y, origin, horizon):
            idx = pd.date_range(origin + syn.H1, periods=horizon, freq="h")
            return pd.DataFrame({"p10": -5.0, "p50": -1.0, "p90": 0.0}, index=idx)

    monkeypatch.setattr(runner, "get_forecaster",
                        lambda *_a, **_k: (Negative(), {"trained_until": syn.DATA_END}, "loaded"))
    fc, described, note = forecast_section(ctx, "North", AFTER_DATA, "gbm")
    assert described == "naive (fallback)"
    assert "physically plausible" in note
    assert (fc[["p10", "p50", "p90"]] >= 0).all().all()


def test_naive_forecast_needs_no_model(ctx):
    fc, described, note = forecast_section(ctx, "South", AFTER_DATA, "naive")
    assert (described, note) == ("naive", None)
    assert len(fc) == ctx.cfg.ops.horizon_hours


# ----------------------------------------------------------------------------- alerts
def test_plan_alerts_cover_storage_solver_and_forecast_problems(tmp_path, north_plan):
    store = PlanStore(tmp_path / "s.sqlite")
    plan = replace(north_plan, status="fallback",
                   notes=["ALERT: storage falls below the floor at 14:00.", "Warning: under the reserve for 3 h."],
                   summary={**north_plan.summary, "forecaster": "naive (fallback)", "message": "solver crashed"})
    alerts = plan_alerts(store, "North", plan, syn.PLAN_AT)
    assert [a["key"] for a in alerts] == ["storage_floor", "storage_min", "milp_fallback", "forecast_fallback"]
    assert [a["level"] for a in alerts] == ["alert", "warning", "warning", "warning"]
    assert alerts[0]["message"] == "North: storage falls below the floor at 14:00."
    assert alerts[1]["message"] == "North: under the reserve for 3 h."
    assert "solver crashed" in alerts[2]["message"]
    assert all(a["new"] for a in alerts)
    assert not any(a["new"] for a in plan_alerts(store, "North", plan, syn.PLAN_AT + syn.H1))


def test_send_webhook_needs_a_url_and_alerts_and_survives_failures(ctx, monkeypatch):
    alert = {"section": "North", "level": "alert", "key": "k", "message": "m", "plan_time": "t", "new": True}
    assert send_webhook(ctx.cfg, [alert]) is False  # no URL configured
    cfg = replace(ctx.cfg, live=replace(ctx.cfg.live, alert_webhook=WEBHOOK))
    assert send_webhook(cfg, []) is False

    def unreachable(*_a, **_k):
        raise requests.ConnectionError("unreachable")

    monkeypatch.setattr(requests, "post", unreachable)
    logs: list[str] = []
    assert send_webhook(cfg, [alert], log=logs.append) is False
    assert any("alert webhook failed" in m for m in logs)


# ----------------------------------------------------------------------------- cycles
def test_run_once_plans_then_flags_stale_data_once(ctx, tmp_path, posts):
    c = syn.ctx_in(ctx, tmp_path, alert_webhook=WEBHOOK)
    store = PlanStore(c.store_path())
    logs: list[str] = []

    first = run_once(c, store, AFTER_DATA, forecaster_kind="naive", log=logs.append)
    assert first.status == "ok", first.message
    assert set(first.plans) == set(store.latest_plans()) == {"North", "South"}
    sent = [a for a in first.alerts if a["new"] and a["level"] in ("warning", "alert")]
    assert len(posts) == (1 if sent else 0)
    before = len(posts)

    stale = run_once(c, store, AFTER_DATA + 10 * syn.H1, forecaster_kind="naive", log=logs.append)
    assert (stale.status, stale.message, stale.plans) == ("stale", "no plans made", {})
    keys = [a["key"] for a in stale.alerts]
    assert keys == ["stale_data", "stale_data"] and all(a["new"] for a in stale.alerts)
    assert "no pump telemetry since 2019-04-10 23:00" in stale.alerts[0]["message"]
    assert len(posts) == before + 1
    post = posts[-1]
    assert post["url"] == WEBHOOK
    assert post["json"]["site"] == c.cfg.name
    assert post["json"]["text"].startswith(f"{c.cfg.name}\n[ALERT] ")
    assert [a["key"] for a in post["json"]["alerts"]] == ["stale_data", "stale_data"]

    again = run_once(c, store, AFTER_DATA + 11 * syn.H1, forecaster_kind="naive", log=logs.append)
    assert again.status == "stale"
    assert not any(a["new"] for a in again.alerts)
    assert len(posts) == before + 1  # an open alert is not re-sent
    assert list(store.recent_runs()["status"]) == ["stale", "stale", "ok"]
    # The app keeps showing the last good plans.
    assert store.latest_plans()["North"].timeline.index[0] == AFTER_DATA


def test_a_failing_cycle_is_recorded_and_alerted(ctx, tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "make_plans", boom)
    store = PlanStore(tmp_path / "s.sqlite")
    logs: list[str] = []
    res = run_once(ctx, store, AFTER_DATA, forecaster_kind="naive", log=logs.append)
    assert (res.status, res.message) == ("error", "RuntimeError: boom")
    alert = store.recent_alerts().iloc[0]
    assert alert["key"] == "runner_error" and pd.isna(alert["section"])
    assert store.recent_runs().iloc[0]["status"] == "error"
    assert any("Traceback" in m for m in logs)


def test_weather_is_stored_and_a_weather_failure_does_not_stop_planning(ctx, tmp_path):
    class Weather:
        def forecast(self):
            idx = pd.date_range(AFTER_DATA, periods=2, freq="h", name="time")
            return pd.DataFrame({"temp_f": [80.0, 82.0], "precip_prob": [0.0, 0.1]}, index=idx)

    class Offline:
        def forecast(self):
            raise requests.ConnectionError("offline")

    store = PlanStore(tmp_path / "s.sqlite")
    logs: list[str] = []
    ok = run_once(ctx, store, AFTER_DATA, forecaster_kind="naive", method="rule", log=logs.append, weather=Weather())
    assert ok.status == "ok"
    assert list(store.load_weather()["temp_f"]) == [80.0, 82.0]
    still = run_once(ctx, store, AFTER_DATA, forecaster_kind="naive", method="rule", log=logs.append,
                     weather=Offline())
    assert still.status == "ok"
    assert any("weather fetch failed" in m for m in logs)


def test_prune_store_never_raises(ctx, tmp_path):
    class Broken:
        def prune(self, **_kw):
            raise OSError("disk full")

    logs: list[str] = []
    assert prune_store(Broken(), ctx.cfg, AFTER_DATA, log=logs.append) is None
    assert any("store pruning failed" in m for m in logs)
    assert prune_store(PlanStore(tmp_path / "s.sqlite"), ctx.cfg, AFTER_DATA) == {"plans": 0, "runs": 0, "hourly": 0}


# ----------------------------------------------------------------------------- the loop
def test_replay_loop_carries_storage_forward(site):
    logs: list[str] = []
    run_loop(site / "config" / "site.toml", source="csv", replay_start="2019-04-09 00:00", steps=2,
             forecaster_kind="naive", method="rule", log=logs.append)
    store = PlanStore(site / "var" / "test.sqlite")
    runs = store.recent_runs()
    assert list(runs["status"]) == ["ok", "ok"], logs
    assert list(runs["plan_time"]) == ["2019-04-09 01:00:00", "2019-04-09 00:00:00"]
    assert set(runs["source"]) == {"csv-replay"}
    history = store.plan_history("North")
    assert list(history["plan_start"]) == ["2019-04-09 01:00:00", "2019-04-09 00:00:00"]
    assert history["storage_source"].iloc[-1] == CONFIGURED
    assert history["storage_source"].iloc[0] == "carried forward from the plan made for Tue 00:00"


def test_pi_source_needs_a_tag_map_and_a_url(site):
    config = site / "config" / "site.toml"
    with pytest.raises(FileNotFoundError):
        run_loop(config, source="pi", once=True, log=lambda _m: None)
    (site / "config" / "pi_tags.csv").write_text("column,pi_path\nPMP_1_FLOW,\\\\PI\\WELL01.FLOW\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pi_base_url"):
        run_loop(config, source="pi", once=True, log=lambda _m: None)
    with pytest.raises(ValueError, match="unknown source"):
        run_loop(config, source="ftp", log=lambda _m: None)
