"""The Dash app: pages load and each server callback renders from the store and the reports.

Callbacks are driven through Dash's own HTTP endpoint with Flask's test client, the way
the browser calls them, so no server or browser is started.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import plotly.graph_objects as go
import pytest

from tests import synthetic as syn
from wellopt.dashboard import (box, create_app, demand_figure, map_figure, merit_order, on_peak_periods,
                               schedule_figure, storage_figure)
from wellopt.diagnostics import audit, write_audit
from wellopt.live.store import PlanStore
from wellopt.reporting import write_backtest

FIGS = ["fig-demand", "fig-storage", "fig-schedule", "fig-map"]
ALERT_TEXT = "North: under the reserve for 2 h."


@pytest.fixture(scope="module")
def dash_env(tmp_path_factory, site_template, ctx, north_plan, small_backtest):
    root = tmp_path_factory.mktemp("dash")
    store = PlanStore(root / "store.sqlite")
    store.save_plans({"North": north_plan}, data_end=syn.PLAN_AT - syn.H1)
    store.add_alert("North", "warning", "storage_min", ALERT_TEXT, syn.PLAN_AT)
    c = syn.ctx_in(ctx, root)
    table, results = small_backtest
    write_backtest(table, results, c, syn.BT_START, syn.BT_END, 6)
    write_audit(audit(ctx.qc, ctx.table, ctx.cfg), c.reports_dir())
    config = site_template / "config" / "site.toml"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("WELLOPT_BOOTSTRAP_CSS", "none")  # no CDN stylesheet
        app = create_app(config, store_path=store.path, reports_dir=c.reports_dir())
        empty = create_app(config, store_path=root / "missing.sqlite", reports_dir=root / "no-reports")
    return SimpleNamespace(app=app, client=app.server.test_client(), empty=empty.server.test_client(), store=store)


def dep(id_, prop, value=None) -> dict:
    return {"id": id_, "property": prop, "value": value}


def dispatch(env, first_output: str, inputs, state=(), changed=(), client=None):
    """Call the callback whose first output is `first_output`. Returns {id: {prop: value}},
    or None when the callback declined to update (HTTP 204)."""
    key = next(k for k in env.app.callback_map if k == first_output or k.startswith(f"..{first_output}..."))
    parts = key[2:-2].split("...") if key.startswith("..") else [key]
    outputs = [dict(zip(("id", "property"), p.rsplit(".", 1))) for p in parts]
    body = {"output": key, "outputs": outputs if key.startswith("..") else outputs[0], "inputs": list(inputs),
            "state": list(state), "changedPropIds": list(changed)}
    r = (client or env.client).post("/_dash-update-component", json=body)
    if r.status_code == 204:
        return None
    assert r.status_code == 200, r.get_data(as_text=True)[:2000]
    return r.get_json()["response"]


def n_props(resp: dict) -> int:
    return sum(len(v) for v in resp.values())


def text(obj) -> str:
    return json.dumps(obj)


def stored(env):
    resp = dispatch(env, "plans.data", [dep("tick", "n_intervals", 0), dep("ack-done", "data")])
    return resp["plans"]["data"], resp["alerts"]["data"], resp["runs"]["data"]


def live(env, plans, theme="light", whatif=None, client=None):
    return dispatch(env, "live-empty.style", [dep("plans", "data", plans), dep("whatif", "data", whatif),
                                              dep("live-section", "value", "North"), dep("theme", "data", theme)],
                    client=client)


# ----------------------------------------------------------------------------- pages
def test_pages_load(dash_env):
    assert dash_env.client.get("/").status_code == 200
    layout = dash_env.client.get("/_dash-layout")
    assert layout.status_code == 200
    assert "Well pump scheduler" in layout.get_data(as_text=True)


def test_store_contents_reach_the_page(dash_env):
    plans, alerts, runs = stored(dash_env)
    assert list(plans) == ["North"]
    assert plans["North"]["summary"]["plan_id"] == 1
    assert [a["message"] for a in alerts] == [ALERT_TEXT]
    assert [r["status"] for r in runs] == ["ok"]


def test_no_store_yet(dash_env):
    resp = dispatch(dash_env, "plans.data", [dep("tick", "n_intervals", 0), dep("ack-done", "data")],
                    client=dash_env.empty)
    assert (resp["plans"]["data"], resp["alerts"]["data"], resp["runs"]["data"]) == ({}, [], [])


# ----------------------------------------------------------------------------- live tab
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_live_tab_renders_the_plan(dash_env, theme):
    plans, _, _ = stored(dash_env)
    resp = live(dash_env, plans, theme)
    assert n_props(resp) == 17
    assert resp["live-empty"]["style"] == {"display": "none"} and resp["live-main"]["style"] == {}
    for f in FIGS:
        height = resp[f]["figure"]["layout"]["height"]
        assert resp[f]["style"] == {"height": f"{int(height)}px"}  # the box carries the figure's height
    assert resp["whatif-storage"]["placeholder"] == "0.36 (stored plan)"
    assert sorted(o["value"] for o in resp["whatif-pumps"]["options"]) == ["PMP_1", "PMP_2", "PMP_3"]
    assert resp["whatif-banner"]["children"] is None
    assert "Storage is ASSUMED" in text(resp["live-notes"]["children"])


def test_dark_theme_changes_the_chart_colors(dash_env):
    plans, _, _ = stored(dash_env)
    light, dark = live(dash_env, plans, "light"), live(dash_env, plans, "dark")
    bg = [r["fig-demand"]["figure"]["layout"]["paper_bgcolor"] for r in (light, dark)]
    assert bg[0] != bg[1]


def test_live_tab_without_plans(dash_env):
    resp = live(dash_env, {})
    assert resp["live-main"]["style"] == {"display": "none"} and resp["live-empty"]["style"] == {}
    assert all(resp[f]["style"]["height"].endswith("px") for f in FIGS)


def test_what_if_run_and_reset(dash_env):
    plans, _, _ = stored(dash_env)
    state = [dep("plans", "data", plans), dep("live-section", "value", "North"), dep("whatif-storage", "value", 0.5),
             dep("whatif-pumps", "value", ["PMP_1"]), dep("whatif-method", "value", "milp"), dep("whatif", "data")]
    run = [dep("whatif-run", "n_clicks", 1), dep("whatif-reset", "n_clicks")]
    resp = dispatch(dash_env, "whatif.data", run, state, changed=["whatif-run.n_clicks"])
    assert resp["whatif-error"]["children"] == ""
    w = resp["whatif"]["data"]["North"]
    assert w["base_plan_id"] == plans["North"]["summary"]["plan_id"]
    assert "storage now 0.50 MG" in w["described"] and "also out of service: PMP_1" in w["described"]
    assert w["record"]["summary"]["storage_now_mg"] == pytest.approx(0.5)

    shown = live(dash_env, plans, whatif=resp["whatif"]["data"])
    assert "What-if: not saved, not sent" in text(shown["whatif-banner"]["children"])

    state[-1] = dep("whatif", "data", resp["whatif"]["data"])
    reset = dispatch(dash_env, "whatif.data", [dep("whatif-run", "n_clicks", 1), dep("whatif-reset", "n_clicks", 1)],
                     state, changed=["whatif-reset.n_clicks"])
    assert reset["whatif"]["data"] == {}


def test_what_if_without_a_plan(dash_env):
    state = [dep("plans", "data", {}), dep("live-section", "value", "North"), dep("whatif-storage", "value"),
             dep("whatif-pumps", "value", []), dep("whatif-method", "value", "milp"), dep("whatif", "data")]
    resp = dispatch(dash_env, "whatif.data", [dep("whatif-run", "n_clicks", 1), dep("whatif-reset", "n_clicks")],
                    state, changed=["whatif-run.n_clicks"])
    assert "whatif" not in resp  # left unchanged
    assert "No plan" in text(resp["whatif-error"]["children"])


# ----------------------------------------------------------------------------- alerts
def test_alerts_and_runs_lists(dash_env):
    _, alerts, runs = stored(dash_env)
    resp = dispatch(dash_env, "alerts-list.children",
                    [dep("alerts", "data", alerts), dep("runs", "data", runs), dep("show-acked", "value", False)])
    assert ALERT_TEXT in text(resp["alerts-list"]["children"])
    assert "Acknowledge" in text(resp["alerts-list"]["children"])
    assert "Plan hour" in text(resp["live-runs"]["children"])
    none = dispatch(dash_env, "alerts-list.children",
                    [dep("alerts", "data", []), dep("runs", "data", []), dep("show-acked", "value", False)])
    assert "No open alerts." in text(none["alerts-list"]["children"])
    assert "No runs recorded." in text(none["live-runs"]["children"])


def test_acknowledging_an_alert(dash_env):
    alert = dash_env.store.add_alert("North", "alert", "test_ack", "North: test alert.", syn.PLAN_AT)
    button = {"index": alert["id"], "type": "ack"}
    prop = json.dumps(button, sort_keys=True, separators=(",", ":")) + ".n_clicks"
    resp = dispatch(dash_env, "ack-done.data", [[dep(button, "n_clicks", 1)]], changed=[prop])
    assert isinstance(resp["ack-done"]["data"], float)
    row = dash_env.store.recent_alerts().set_index("id").loc[alert["id"]]
    assert row["acknowledged"] == 1


# ----------------------------------------------------------------------------- report tabs
def backtest(env, tab, client=None):
    return dispatch(env, "bt-empty.style", [dep("bt-section", "value", "North"), dep("theme", "data", "light"),
                                            dep("tabs", "active_tab", tab)], client=client)


def audit_tab(env, tab, client=None):
    return dispatch(env, "audit-empty.style", [dep("theme", "data", "dark"), dep("tabs", "active_tab", tab)],
                    client=client)


def test_backtest_tab(dash_env):
    resp = backtest(dash_env, "backtest")
    assert n_props(resp) == 13
    assert resp["bt-main"]["style"] == {}
    assert "best runnable policy" in text(resp["bt-findings"])
    for f in ("fig-bt-savings", "fig-bt-cost", "fig-bt-storage"):
        assert resp[f]["style"]["height"] == f"{int(resp[f]['figure']['layout']['height'])}px"
    assert backtest(dash_env, "live") is None  # hidden tab: not drawn
    assert backtest(dash_env, "backtest", client=dash_env.empty)["bt-main"]["style"] == {"display": "none"}


def test_audit_tab(dash_env):
    resp = audit_tab(dash_env, "audit")
    assert n_props(resp) == 9
    assert resp["audit-main"]["style"] == {}
    assert "OK" in text(resp["audit-verdict"]["children"])
    assert audit_tab(dash_env, "backtest") is None
    assert audit_tab(dash_env, "audit", client=dash_env.empty)["audit-main"]["style"] == {"display": "none"}


# ----------------------------------------------------------------------------- figure helpers
def test_box_carries_the_figure_height():
    assert box(300) == {"height": "300px"}
    assert box(go.Figure(layout={"height": 250})) == {"height": "250px"}
    assert box(go.Figure()) == {"height": "320px"}


def test_plan_figures(north_plan):
    assert on_peak_periods(north_plan.timeline) == [(syn.PLAN_AT + 14 * syn.H1, syn.PLAN_AT + 20 * syn.H1)]
    assert sorted(merit_order(north_plan)) == ["PMP_1", "PMP_2", "PMP_3"]
    for build in (demand_figure, storage_figure, schedule_figure, map_figure):
        for theme in ("light", "dark"):
            fig = build(north_plan, theme)
            assert isinstance(fig, go.Figure) and fig.layout.height and len(fig.data) > 0
