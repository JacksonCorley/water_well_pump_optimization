"""The ops page: pump marks in the store, re-planning around them, what the page says about each pump, how
its pump-curve chart is framed, and its JSON routes, driven with Flask's test client.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tests import synthetic as syn
from wellopt.config import load_config
from wellopt.curves import load_pump_curves
from wellopt.live.runner import make_plans
from wellopt.live.store import PlanStore
from wellopt.ops_page import axis_range, chart_frame, create_app, page_views, plan_age
from wellopt.ops_view import EMPTY, NO_CURVE_NOTE, OpsData, next_change, pump_action, section_view
from wellopt.scheduler import replan

READ_AT = syn.PLAN_AT - syn.H1  # the readings the plans start from
PMP_1_CURVE = [(500, 380, 40), (1000, 350, 65), (1500, 315, 80), (2000, 270, 72), (2500, 210, 55)]  # BEP 1576
PMP_3_CURVE = [(400, 340, 40), (700, 315, 62), (1000, 290, 76), (1300, 255, 72), (1600, 210, 58)]  # BEP 1083
CURVES = ("pump,flow_gpm,head_ft,efficiency_pct,source\n"
          + "".join(f"PMP_1,{q},{h},{e},made-up test curve\n" for q, h, e in PMP_1_CURVE)
          + "".join(f"PMP_3,{q},{h},{e},pump test 2018\n" for q, h, e in PMP_3_CURVE))


def row(view: dict, pump: str) -> dict:
    return next(r for r in view["rows"] if r["pump"] == pump)


@pytest.fixture(scope="module")
def ops_site(tmp_path_factory, site_template):
    """A copy of the synthetic site with maker curves for PMP_1 and PMP_3."""
    root = tmp_path_factory.mktemp("ops") / "site"
    shutil.copytree(site_template, root)
    (root / "config" / "pump_curves.csv").write_text(CURVES, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def cfg(ops_site):
    return load_config(ops_site / "config" / "site.toml")


@pytest.fixture(scope="module")
def curves(ops_site):
    return load_pump_curves(ops_site / "config" / "pump_curves.csv")


@pytest.fixture(scope="module")
def north_view(north_plan, ctx, curves, cfg) -> dict:
    return section_view(north_plan, ctx, curves, {}, cfg)


def stored(path, *plans) -> PlanStore:
    store = PlanStore(path)
    for plan in plans:
        store.save_plans({plan.section: plan}, data_end=READ_AT)
    return store


@pytest.fixture
def ops(cfg, north_plan, tmp_path) -> OpsData:
    """The page's data over a fresh store holding the shared North plan."""
    return OpsData(cfg, store_path=stored(tmp_path / "ops.sqlite", north_plan).path)


# ----------------------------------------------------------------------------- marks and re-planning
def test_marks_round_trip_in_the_store(tmp_path):
    store = PlanStore(tmp_path / "s.sqlite")
    assert store.out_of_service() == {}
    store.set_out_of_service("PMP_3", True, "bearing noise", "night shift")
    store.set_out_of_service("PMP_1", True)
    marks = store.out_of_service()
    assert list(marks) == ["PMP_1", "PMP_3"]
    assert (marks["PMP_3"]["note"], marks["PMP_3"]["updated_by"]) == ("bearing noise", "night shift")
    assert (marks["PMP_1"]["note"], marks["PMP_1"]["updated_by"]) == ("", "")
    assert pd.Timestamp(marks["PMP_1"]["updated_at"]) <= pd.Timestamp.now()
    store.set_out_of_service("PMP_3", False)
    assert list(PlanStore(tmp_path / "s.sqlite").out_of_service()) == ["PMP_1"]  # kept on disk


def test_planning_runs_leave_marked_pumps_out(ctx, tmp_path):
    store = PlanStore(tmp_path / "s.sqlite")
    store.set_out_of_service("PMP_3", True, "bearing noise")
    plan = make_plans(syn.ctx_in(ctx, tmp_path), syn.PLAN_AT, "naive", "milp", store=store, sections=["North"],
                      save_models=False)["North"]
    assert plan.inputs["state"]["unavailable"] == ["PMP_3"]
    assert "PMP_3" not in plan.schedule.columns
    assert "Planned without PMP_3 (out of service for this plan)." in plan.notes
    # it was running, so the first hour's note says to stop it
    assert plan.inputs["state"]["on"]["PMP_3"] == 1
    assert re.search(r"stop [^.]*PMP_3 \(out of service\)", plan.notes[0]), plan.notes[0]


def test_replan_takes_pumps_out_and_puts_them_back(ctx, north_plan):
    out = replan(north_plan, ctx.cfg, ctx.tariff, out_of_service=["PMP_3"])
    assert out.inputs["state"]["unavailable"] == ["PMP_3"] and "PMP_3" not in out.schedule.columns
    back = replan(out, ctx.cfg, ctx.tariff, in_service=["PMP_3"])
    assert back.inputs["state"]["unavailable"] == [] and "PMP_3" in back.schedule.columns
    if north_plan.status != "time_limit":  # a time-limited solve may stop at a different incumbent
        pd.testing.assert_frame_equal(back.schedule, north_plan.schedule, check_freq=False, check_dtype=False)


# ----------------------------------------------------------------------------- what the page says
@pytest.mark.parametrize("was_on, planned, oos, action", [
    (False, True, False, "on"), (True, False, False, "off"), (True, True, False, "keep"),
    (False, False, False, "leave"), (False, False, True, "oos"), (True, True, True, "off"),
])
def test_pump_action(was_on, planned, oos, action):
    assert pump_action(was_on, planned, oos) == action


def test_next_change():
    schedule = pd.DataFrame({"A": [1, 1, 0, 0, 1, 1], "B": [0] * 6},
                            index=pd.date_range(syn.PLAN_AT, periods=6, freq="h"))
    assert next_change(schedule, "A") == "on until Mon 02:00"
    assert next_change(schedule, "B") == "off for the whole plan (6 h)"
    assert next_change(schedule, "C") is None


def test_actions_compare_the_first_hour_with_the_hour_before(north_view, north_plan):
    v = north_view
    assert (v["section"], v["readings_at"], v["plan_start"]) == ("North", str(READ_AT), str(syn.PLAN_AT))
    on0, first = north_plan.inputs["state"]["on"], north_plan.schedule.iloc[0]
    for r in v["rows"]:
        assert r["action"] == pump_action(bool(on0[r["pump"]]), bool(first[r["pump"]]), False), r["pump"]
        assert r["next"] == next_change(north_plan.schedule, r["pump"])
    order = ["on", "off", "keep", "leave", "oos"]
    assert [r["action"] for r in v["rows"]] == sorted((r["action"] for r in v["rows"]), key=order.index)
    m = v["metrics"]
    assert m["turn_on"] == [r["pump"] for r in v["rows"] if r["action"] == "on"]
    assert m["turn_off"] == [r["pump"] for r in v["rows"] if r["action"] == "off"]
    assert (m["running_n"], m["planned_n"]) == (sum(on0.values()), int(first.sum()))
    assert m["running_gpm"] == pytest.approx(sum(r["flow"] for r in v["rows"] if r["was_on"]), abs=1)
    assert m["demand_p50"] == pytest.approx(north_plan.timeline["demand_p50"].iloc[0], abs=0.5)
    assert m["produced_gpm"] is not None and not m["storage_measured"]
    assert v["warnings"] == [] and v["notes"] == north_plan.notes
    ranks = sorted(r["rank"] for r in v["rows"])
    assert ranks == [1, 2, 3] and all(r["ranked"] == 3 for r in v["rows"])


def test_a_running_pump_is_placed_on_its_maker_curve(north_view, ctx):
    pv = row(north_view, "PMP_3")
    assert pv["was_on"] and pv["running_now"] and pv["action"] in ("keep", "off")
    assert pv["flow"] == pytest.approx(ctx.qc.flow.at[READ_AT, "PMP_3"], abs=0.5)
    assert pv["head"] == pytest.approx(100 + pv["flow"] / 20 + 2.31 * ctx.qc.press.at[READ_AT, "PMP_3"], abs=1)
    assert pv["maker"]["bep"] == pytest.approx(1083.3, abs=0.1) and pv["maker"]["source"] == "pump test 2018"
    assert pv["check"]["level"] == "good" and pv["check"]["label"] == "Usual"
    assert pv["check"]["pct_bep"] == pytest.approx(100 * pv["flow"] / 1083.3, abs=1)
    assert any("inside the preferred range (70–120%)" in n for n in pv["notes"])
    assert "Maker curve source: pump test 2018." in pv["notes"]
    assert pv["notes"][-1].startswith("Head is estimated: static level 100 ft, plus flow ÷ 20.0 gpm/ft")
    env = pv["env"]
    assert env["hours"] > 100 and len(env["cloud"]) == 300 and env["flow"]["p5"] < pv["flow"] < env["flow"]["p95"]


def test_a_pump_to_start_shows_where_it_should_run(north_view):
    pv = row(north_view, "PMP_1")
    assert pv["action"] == "on" and not pv["was_on"] and pv["flow"] is None and pv["check"] is None
    assert pv["expected"] == [pytest.approx(1500, abs=30), pytest.approx(314, abs=3)]  # its median running point
    assert any(n.startswith("If started, expect about 1,") for n in pv["notes"])
    assert pv["maker"]["bep"] == pytest.approx(1576.1, abs=0.1)


def test_a_pump_without_a_maker_curve(north_view):
    pv = row(north_view, "PMP_2")
    assert pv["maker"] is None and NO_CURVE_NOTE in pv["notes"]


def test_a_mark_made_after_the_plan(north_plan, ctx, curves, cfg):
    marks = {"PMP_3": {"note": "bearing noise", "updated_at": "2019-04-08 00:20:31", "updated_by": "ops page"}}
    v = section_view(north_plan, ctx, curves, marks, cfg)
    pv = row(v, "PMP_3")
    assert pv["oos"] and pv["action"] == "off" and pv["next"] is None and not pv["planned"]
    assert pv["why"] == ("Marked out of service (bearing noise) on 2019-04-08 00:20 by ops page. "
                         "It is running now: turn it off.")
    assert "PMP_3" in v["metrics"]["turn_off"]
    assert v["warnings"] == ["PMP_3 was marked out of service after this plan was made. The next planning run "
                             "leaves it out; until then the planner's numbers still count it."]


def test_a_pump_returned_to_service_after_the_plan(north_plan, ctx, curves, cfg):
    without = replan(north_plan, ctx.cfg, ctx.tariff, out_of_service=["PMP_1"])  # PMP_1 was off
    pv = row(section_view(without, ctx, curves, {}, cfg), "PMP_1")
    assert pv["action"] == "leave" and not pv["oos"]
    assert pv["why"] == "Returned to service after this plan was made; the next plan can use it."


def test_without_readings_the_actions_are_still_shown(north_plan, curves, cfg):
    v = section_view(north_plan, None, curves, {}, cfg)
    assert [r["pump"] for r in v["rows"]] and all(r["flow"] is None and r["env"] is None for r in v["rows"])
    assert v["metrics"]["produced_gpm"] is None


# ----------------------------------------------------------------------------- OpsData
def test_marking_a_pump_re_plans_its_section(ops, north_plan):
    store = ops.store()
    result = ops.mark("PMP_3", True, "  bearing noise ", by="night shift")
    assert (result.section, result.out_of_service, result.replanned) == ("North", True, True)
    assert re.fullmatch(r"PMP_3 marked out of service\. The North plan was redone in \d+\.\d s\.", result.message)
    assert store.out_of_service()["PMP_3"]["note"] == "bearing noise"
    plan = store.latest_plans()["North"]
    assert plan.inputs["state"]["unavailable"] == ["PMP_3"] and "PMP_3" not in plan.schedule.columns
    assert plan.notes[1] == "Re-planned from the ops page after PMP_3 was marked out of service."
    assert "PMP_3 (out of service)" in plan.notes[0]
    for key in ("forecaster", "storage_source", "data_end"):  # carried over from the plan it redoes
        assert plan.summary[key] == north_plan.summary[key]
    assert store.recent_runs(1)["source"].iloc[0] == "ops-page"
    alert = store.recent_alerts(1).iloc[0]
    assert (alert["section"], alert["level"], alert["key"]) == ("North", "info", "out_of_service:PMP_3")
    assert alert["message"] == "PMP_3 marked out of service from the ops page: bearing noise."
    view = ops.views()["sections"]["North"]
    pv = row(view, "PMP_3")
    assert pv["oos"] and pv["action"] == "off" and pv["mark"]["updated_by"] == "night shift"
    assert view["warnings"] == []  # the new plan already leaves it out

    back = ops.mark("PMP_3", False)
    assert back.replanned and back.message.startswith("PMP_3 returned to service. The North plan was redone")
    assert store.out_of_service() == {}
    assert store.latest_plans()["North"].inputs["state"]["unavailable"] == []
    assert store.recent_alerts(1).iloc[0]["message"] == "PMP_3 returned to service from the ops page."
    assert not row(ops.views()["sections"]["North"], "PMP_3")["oos"]


def test_marking_a_pump_no_plan_includes(ops):
    result = ops.mark("PMP_4", True)  # a South pump; the store has only a North plan
    assert (result.section, result.replanned) == (None, False)
    assert result.message == "PMP_4 marked out of service. No stored plan includes it, so nothing was re-planned."
    assert list(ops.store().out_of_service()) == ["PMP_4"]


def test_without_a_store(cfg, tmp_path):
    data = OpsData(cfg, store_path=tmp_path / "none.sqlite")
    assert data.views() == {"sections": {}, "message": EMPTY}
    with pytest.raises(RuntimeError, match="there is no plan store"):
        data.mark("PMP_3", True)
    assert not (tmp_path / "none.sqlite").exists()


def test_a_bad_curve_file_is_reported_until_it_is_fixed(site, north_plan, tmp_path):
    curve_file = site / "config" / "pump_curves.csv"
    curve_file.write_text("pump,flow_gpm,head_ft\nPMP_3,1000,-5\n", encoding="utf-8")
    data = OpsData(load_config(site / "config" / "site.toml"), store_path=stored(tmp_path / "s.sqlite", north_plan).path)
    warnings = data.views()["sections"]["North"]["warnings"]
    assert warnings == ["No maker curves are shown: pump_curves.csv: line 2: head_ft must be positive; "
                        "PMP_3: 1 point(s); give at least 3 points across the curve"]
    curve_file.write_text(CURVES, encoding="utf-8")
    os.utime(curve_file, (curve_file.stat().st_atime, curve_file.stat().st_mtime + 5))
    view = data.views()["sections"]["North"]
    assert view["warnings"] == [] and row(view, "PMP_3")["maker"] is not None


def test_without_the_history_the_plan_is_still_shown(cfg, north_plan, tmp_path):
    data = OpsData(cfg, store_path=stored(tmp_path / "s.sqlite", north_plan).path,
                   history_path=tmp_path / "missing.csv")
    view = data.views()["sections"]["North"]
    assert [r["action"] for r in view["rows"]]
    assert view["warnings"] == [f"The readings behind this plan could not be loaded (no history before "
                                f"{syn.PLAN_AT}), so operating points and pump history are not shown."]


# ----------------------------------------------------------------------------- what the page is sent
def test_axis_range():
    assert axis_range([100, 200]) == [pytest.approx(88), pytest.approx(212)]
    assert axis_range([0, 10]) == [0.0, pytest.approx(11.2)]  # never below zero
    lo, hi = axis_range([5.0])
    assert lo < 5 < hi


def test_the_chart_frames_where_the_pump_runs(north_view, cfg):
    pv = row(north_view, "PMP_3")
    frame = chart_frame(pv, cfg.curves)
    bep = pv["maker"]["bep"]
    assert frame["band"] == [pytest.approx(0.7 * bep), pytest.approx(1.2 * bep)]
    assert frame["bep"] == [bep, pytest.approx(np.interp(bep, pv["maker"]["flow"], pv["maker"]["head"]))]
    (x0, x1), (y0, y1) = frame["x"], frame["y"]
    assert x0 <= min(pv["env"]["flow"]["p1"], 0.7 * bep) and x1 >= max(pv["env"]["flow"]["p99"], 1.2 * bep)
    assert y0 <= pv["head"] <= y1 and y0 <= pv["env"]["head"]["p1"] and y1 >= pv["env"]["head"]["p99"]
    assert x1 < pv["maker"]["flow"][-1]  # the frame does not stretch to the end of the curve


def test_the_chart_for_a_pump_to_start(north_view, cfg):
    pv = row(north_view, "PMP_1")
    frame = chart_frame(pv, cfg.curves)
    (x0, x1), (y0, y1) = frame["x"], frame["y"]
    assert x0 <= pv["expected"][0] <= x1 and y0 <= pv["expected"][1] <= y1


def test_the_chart_without_history():
    base = {"pump": "P", "env": None, "fit": None, "flow": None, "head": None, "expected": None}
    maker = {"flow": [500.0, 1000.0, 1500.0], "head": [300.0, 280.0, 240.0], "eff": None, "source": "", "bep": None}
    whole = chart_frame(dict(base, maker=maker))
    assert whole["x"][0] <= 500 and whole["x"][1] >= 1500  # a maker curve with nothing else to frame is shown whole
    assert whole["y"][0] <= 240 and whole["y"][1] >= 300 and whole["band"] is None and whole["bep"] is None
    assert chart_frame(dict(base, maker=None)) == {"x": None, "y": None, "band": None, "bep": None}
    now_only = chart_frame(dict(base, maker=None, flow=1200.0, head=342.0))
    assert now_only["x"][0] < 1200 < now_only["x"][1] and now_only["y"][0] < 342 < now_only["y"][1]


def test_plan_age():
    now = datetime(2019, 4, 8, 2, 0)
    assert plan_age("2019-04-08 01:15:00", now) == ("45 min ago", 45.0)
    assert plan_age("2019-04-07 23:05:00", now)[0] == "3 h ago"
    assert plan_age("2019-04-05 02:00:00", now)[0] == "3 days ago"
    text, minutes = plan_age(None, now)
    assert text == "time unknown" and math.isnan(minutes)


def test_page_views(ops, cfg):
    made = datetime.fromisoformat(ops.views()["sections"]["North"]["created_at"])
    fresh = page_views(ops, cfg, now=made + timedelta(minutes=45))
    v = fresh["sections"]["North"]
    assert (v["age"], v["stale"]) == ("45 min ago", False)
    old = page_views(ops, cfg, now=made + timedelta(hours=3))["sections"]["North"]
    assert (old["age"], old["stale"]) == ("3 h ago", cfg.live.stale_after_minutes < 180)
    assert fresh["section_names"] == ["North", "South"] and fresh["message"] is None
    assert fresh["loaded_at"] == (made + timedelta(minutes=45)).isoformat(sep=" ", timespec="seconds")
    assert fresh["settings"] == {"por_low_pct": cfg.curves.por_low_pct, "por_high_pct": cfg.curves.por_high_pct,
                                 "stale_after_minutes": cfg.live.stale_after_minutes, "refresh_minutes": 5,
                                 "note_max": 200}
    assert row(v, "PMP_3")["chart"] == chart_frame(row(ops.views()["sections"]["North"], "PMP_3"), cfg.curves)
    json.dumps(fresh, allow_nan=False)  # plain JSON: no NaN and no numpy values


def test_page_views_when_the_store_cannot_be_read(ops, cfg, monkeypatch):
    def broken():
        raise OSError("disk I/O error")

    monkeypatch.setattr(ops, "views", broken)
    out = page_views(ops, cfg)
    assert out["sections"] == {} and out["message"] == "The plan store could not be read (OSError: disk I/O error)."


# ----------------------------------------------------------------------------- the page and its routes
def page_env(site, store_path) -> SimpleNamespace:
    app = create_app(site / "config" / "site.toml", store_path=store_path)
    return SimpleNamespace(app=app, client=app.test_client(), data=app.extensions["wellopt_ops"])


@pytest.fixture(scope="module")
def page(ops_site, north_plan, tmp_path_factory):
    return page_env(ops_site, stored(tmp_path_factory.mktemp("ops-page") / "store.sqlite", north_plan).path)


def post_mark(client, body, **kw):
    return client.post("/api/marks", json=body, **kw)


def test_the_page_loads(page):
    r = page.client.get("/")
    html = r.get_data(as_text=True)
    r.close()
    assert r.status_code == 200 and r.mimetype == "text/html"
    assert "<title>Well Pump Operations Support</title>" in html
    assert 'src="static/ops.js"' in html and 'href="static/ops.css"' in html
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "default-src 'self'" in csp and r.headers["X-Content-Type-Options"] == "nosniff"
    assert "https://" not in html
    for name, mimetype in (("ops.js", "text/javascript"), ("ops.css", "text/css")):
        s = page.client.get(f"/static/{name}")
        text = s.get_data(as_text=True)
        s.close()
        assert s.status_code == 200 and s.mimetype == mimetype, name  # nosniff refuses a script sent as text/plain
        assert "https://" not in text and "http://" not in text, name  # nothing is loaded from the internet


def test_the_page_ships_its_fonts(page):
    css = page.client.get("/static/ops.css")
    fonts = re.findall(r'url\("(fonts/[^"]+)"\)', css.get_data(as_text=True))
    css.close()
    assert len(fonts) == 6  # Plex Sans and Plex Mono at 400, 500 and 600
    for path in fonts:
        f = page.client.get(f"/static/{path}")
        data = f.get_data()
        f.close()
        assert f.status_code == 200 and f.mimetype == "font/woff2" and data[:4] == b"wOF2", path
    lic = page.client.get("/static/fonts/LICENSE.txt")
    assert lic.status_code == 200 and b"SIL Open Font License" in lic.get_data()
    lic.close()


def test_the_views_route(page):
    r = page.client.get("/api/views")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    views = r.get_json()
    assert list(views["sections"]) == ["North"] and views["message"] is None and views["loaded_at"]
    assert views["section_names"] == ["North", "South"] and views["settings"]["note_max"] == 200
    v = views["sections"]["North"]
    assert v["age"].endswith("ago") and isinstance(v["stale"], bool)
    pv = row(v, "PMP_3")
    assert pv["chart"]["band"] and pv["maker"]["bep"] == pytest.approx(1083.3, abs=0.1)


def test_the_views_route_without_plans(ops_site, tmp_path):
    env = page_env(ops_site, tmp_path / "none.sqlite")
    views = env.client.get("/api/views").get_json()
    assert views["sections"] == {} and views["message"] == EMPTY
    assert not (tmp_path / "none.sqlite").exists()


def test_the_routes_when_the_store_cannot_be_read(page, monkeypatch):
    def broken():
        raise OSError("disk I/O error")

    monkeypatch.setattr(page.data, "views", broken)
    views = page.client.get("/api/views").get_json()
    assert views["sections"] == {} and views["message"] == "The plan store could not be read (OSError: disk I/O error)."
    r = post_mark(page.client, {"pump": "PMP_3", "out_of_service": True})
    assert r.status_code == 503 and r.get_json()["error"].startswith("The plan store could not be read")


def test_marking_and_returning_a_pump_from_the_page(ops_site, north_plan, tmp_path):
    store = stored(tmp_path / "store.sqlite", north_plan)
    env = page_env(ops_site, store.path)
    r = post_mark(env.client, {"pump": "PMP_3", "out_of_service": True, "note": "bearing noise"})
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    body = r.get_json()
    assert body["message"].startswith("PMP_3 marked out of service. The North plan was redone in ")
    assert (body["pump"], body["out_of_service"], body["section"], body["replanned"]) == ("PMP_3", True, "North", True)
    mark = store.out_of_service()["PMP_3"]
    assert (mark["note"], mark["updated_by"]) == ("bearing noise", "ops page")
    pv = row(body["views"]["sections"]["North"], "PMP_3")  # the answer carries the new plan
    assert pv["action"] == "off" and pv["mark"]["note"] == "bearing noise"

    r = post_mark(env.client, {"pump": "PMP_3", "out_of_service": False, "note": "ignored"})
    assert r.status_code == 200 and r.get_json()["message"].startswith("PMP_3 returned to service.")
    assert store.out_of_service() == {}
    assert not row(r.get_json()["views"]["sections"]["North"], "PMP_3")["oos"]


@pytest.mark.parametrize("kwargs, status, words", [
    ({"data": "pump=PMP_3&out_of_service=1", "content_type": "application/x-www-form-urlencoded"}, 415,
     "Send the mark as JSON."),
    ({"data": '{"pump": "PMP_3", "out_of_service": true}', "content_type": "text/plain"}, 415, "Send the mark as JSON."),
    ({"json": {"pump": "PMP_3", "out_of_service": True}, "headers": {"Sec-Fetch-Site": "cross-site"}}, 403,
     "Pumps can only be marked from this page."),
    ({"data": "{not json", "content_type": "application/json"}, 400, 'Send {"pump": "PMP_3"'),
    ({"json": ["PMP_3"]}, 400, 'Send {"pump": "PMP_3"'),
    ({"json": {"pump": "PMP_3", "out_of_service": "yes"}}, 400, 'Send {"pump": "PMP_3"'),
    ({"json": {"pump": "PMP_3", "out_of_service": True, "note": 5}}, 400, 'Send {"pump": "PMP_3"'),
    ({"json": {"pump": "PMP_3", "out_of_service": True, "note": "x" * 201}}, 400, "Keep the reason to 200 characters."),
    ({"json": {"pump": "PMP_3", "out_of_service": True, "note": "x" * 20_000}}, 413, "The mark is too large."),
    ({"json": {"pump": "PMP_9", "out_of_service": True}}, 409, "PMP_9 is not in the newest plan; refresh the page."),
    ({"json": {"pump": "PMP_4", "out_of_service": True}}, 409, "PMP_4 is not in the newest plan"),  # South: no plan
    ({"json": {"pump": "PMP_3", "out_of_service": False}}, 409, "PMP_3 is not marked out of service; refresh the page."),
])
def test_the_marks_route_checks_what_it_is_sent(page, kwargs, status, words):
    r = page.client.post("/api/marks", **kwargs)
    assert r.status_code == status and words in r.get_json()["error"]
    assert page.data.store().out_of_service() == {}  # nothing was saved


def test_the_page_will_not_mark_a_pump_it_cannot_plan(page, monkeypatch):
    views = copy.deepcopy(page.data.views())
    row(views["sections"]["North"], "PMP_2").update(
        oos=True, mark=None, why="Not planned: the history has no usable flow and power readings for it.")
    monkeypatch.setattr(page.data, "views", lambda: views)
    r = post_mark(page.client, {"pump": "PMP_2", "out_of_service": True})
    assert r.status_code == 409 and r.get_json()["error"].startswith("PMP_2 cannot be planned (Not planned: the history")
    assert page.data.store().out_of_service() == {}


def test_a_mark_that_cannot_be_saved(page, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(page.data, "mark", locked)
    r = post_mark(page.client, {"pump": "PMP_3", "out_of_service": True})
    assert r.status_code == 500 and r.get_json()["error"] == "The mark was not saved: OperationalError: database is locked"
