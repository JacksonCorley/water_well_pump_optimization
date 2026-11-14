"""Live data sources and the weather client (wellopt.live.sources, wellopt.live.weather).

The PI Web API and NWS clients are exercised against fake HTTP sessions: no request
leaves the machine.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests import synthetic as syn
from wellopt.live.sources import CsvReplaySource, PIWebAPISource, load_tag_map
from wellopt.live.weather import NWS_API, NWSHourly

PI = "https://pi.example.invalid/piwebapi"
TAGS = {"PMP_1_FLOW": r"\\PI\WELL01.FLOW", "PMP_1_PRESS": r"\\PI\WELL01.PRESS"}
TZ = "America/Chicago"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    """Answers GETs from `route(url, params)` and records every call."""

    def __init__(self, route):
        self.route, self.calls = route, []
        self.headers: dict = {}
        self.auth = self.verify = None

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        return FakeResponse(self.route(url, params or {}))


# ----------------------------------------------------------------------------- CSV replay
@pytest.fixture(scope="module")
def raw():
    return syn.make_history()


def test_replay_clock_and_fetch_never_see_the_future(raw):
    src = CsvReplaySource(raw, start="2019-04-09 00:30")
    assert src.now() == pd.Timestamp("2019-04-09 00:00")
    rows = src.fetch("2019-04-08 20:00", "2019-04-09 06:00")
    assert list(rows.index) == list(pd.date_range("2019-04-08 20:00", periods=4, freq="h"))
    src.advance(2)
    assert src.now() == pd.Timestamp("2019-04-09 02:00")
    assert src.fetch("2019-04-08 20:00", "2019-04-09 06:00").index.max() == pd.Timestamp("2019-04-09 01:00")


def test_replay_start_defaults_and_warm_up(raw):
    assert CsvReplaySource(raw).now() == syn.DATA_END - pd.Timedelta(days=7)
    assert CsvReplaySource(raw, start="2019-01-05").now() == syn.START + pd.Timedelta(days=35)


def test_replay_ends_an_hour_after_the_data(raw):
    src = CsvReplaySource(raw, start=syn.DATA_END + syn.H1)
    assert not src.exhausted
    src.advance()
    assert src.exhausted


def test_tag_map(tmp_path):
    path = tmp_path / "tags.csv"
    path.write_text("column,pi_path\n PMP_1_FLOW , \\\\PI\\WELL01.FLOW \n,\nPMP_2_FLOW,\n"
                    "PMP_1_PRESS,\\\\PI\\WELL01.PRESS\n", encoding="utf-8")
    assert load_tag_map(path) == TAGS


# ----------------------------------------------------------------------------- PI Web API
def pi_route(items_by_web_id: dict):
    def route(url, params):
        if url == f"{PI}/points":
            return {"WebId": "W-" + params["path"].rsplit(".", 1)[-1]}
        web_id = url.split("/streams/")[1].split("/")[0]
        return {"Items": items_by_web_id[web_id]}
    return route


def item(utc: str, value, good: bool = True) -> dict:
    return {"Value": {"Timestamp": utc, "Value": value, "Good": good}}


def test_pi_settings_and_credentials_from_the_environment(monkeypatch):
    monkeypatch.setenv("WELLOPT_PI_USER", "reader")
    monkeypatch.setenv("WELLOPT_PI_PASSWORD", "test-only")
    session = FakeSession(pi_route({}))
    src = PIWebAPISource(PI + "/", TAGS, TZ, session=session, verify="ca.pem")
    assert src.base_url == PI
    assert session.auth == ("reader", "test-only")
    assert session.verify == "ca.pem"
    assert session.headers["Accept"] == "application/json"


def test_pi_needs_a_url_and_tags():
    with pytest.raises(ValueError, match="pi_base_url"):
        PIWebAPISource("", TAGS, TZ, session=FakeSession(pi_route({})))
    with pytest.raises(ValueError, match="tag map is empty"):
        PIWebAPISource(PI, {}, TZ, session=FakeSession(pi_route({})))


def test_pi_hourly_averages_in_local_time():
    items = [item("2019-04-08T05:00:00Z", 1000.0),
             item("2019-04-08T06:00:00Z", 1010.0, good=False),               # bad quality
             item("2019-04-08T07:00:00Z", {"Name": "Shutdown", "Value": 248})]  # digital state
    session = FakeSession(pi_route({"W-FLOW": items}))
    src = PIWebAPISource(PI, TAGS, TZ, session=session)
    s = src.fetch_series(TAGS["PMP_1_FLOW"], "2019-04-08 00:00", "2019-04-08 03:00")
    assert list(s.index) == list(pd.date_range("2019-04-08 00:00", periods=3, freq="h"))  # 05:00Z = 00:00 CDT
    np.testing.assert_array_equal(s.to_numpy(), [1000.0, np.nan, np.nan])
    url, params, _ = session.calls[-1]
    assert url == f"{PI}/streams/W-FLOW/summary"
    assert params == {"startTime": "2019-04-08T05:00:00Z", "endTime": "2019-04-08T08:00:00Z",
                      "summaryType": "Average", "summaryDuration": "1h", "calculationBasis": "TimeWeighted",
                      "timeType": "EarliestTime"}
    src.fetch_series(TAGS["PMP_1_FLOW"], "2019-04-08 00:00", "2019-04-08 03:00")
    assert sum(1 for u, *_ in session.calls if u.endswith("/points")) == 1  # the web id is cached


def test_pi_repeated_autumn_hour_keeps_its_first_value():
    items = [item("2019-11-03T05:00:00Z", 1.0), item("2019-11-03T06:00:00Z", 2.0),   # 00:00, 01:00 CDT
             item("2019-11-03T07:00:00Z", 3.0), item("2019-11-03T08:00:00Z", 4.0)]   # 01:00, 02:00 CST
    src = PIWebAPISource(PI, TAGS, TZ, session=FakeSession(pi_route({"W-FLOW": items})))
    s = src.fetch_series(TAGS["PMP_1_FLOW"], "2019-11-03 00:00", "2019-11-03 03:00")
    assert list(s.index) == list(pd.DatetimeIndex(["2019-11-03 00:00", "2019-11-03 01:00", "2019-11-03 02:00"]))
    assert s.tolist() == [1.0, 2.0, 4.0]


def test_pi_fetch_builds_an_export_frame():
    hours = [f"2019-04-08T{h:02d}:00:00Z" for h in (5, 6, 7)]
    session = FakeSession(pi_route({"W-FLOW": [item(t, 100.0 + i) for i, t in enumerate(hours)],
                                    "W-PRESS": [item(t, 60.0) for t in hours]}))
    df = PIWebAPISource(PI, TAGS, TZ, session=session).fetch("2019-04-08 00:00", "2019-04-08 02:00")
    assert df.index.name == "time"
    assert list(df.columns) == list(TAGS)
    assert list(df.index) == list(pd.date_range("2019-04-08 00:00", periods=2, freq="h"))  # end is exclusive
    assert df["PMP_1_FLOW"].tolist() == [100.0, 101.0]
    empty = PIWebAPISource(PI, TAGS, TZ, session=session).fetch("2019-04-08 02:00", "2019-04-08 02:00")
    assert empty.empty and list(empty.columns) == list(TAGS)


# ----------------------------------------------------------------------------- NWS weather
HOURLY = f"{NWS_API}/gridpoints/XYZ/10,20/forecast/hourly"


def nws_route(periods):
    def route(url, _params):
        if url == f"{NWS_API}/points/38.0000,-97.0000":
            return {"properties": {"forecastHourly": HOURLY}}
        assert url == HOURLY
        return {"properties": {"periods": periods}}
    return route


def test_nws_needs_a_user_agent():
    with pytest.raises(ValueError, match="User-Agent"):
        NWSHourly(38.0, -97.0, "", TZ, session=FakeSession(nws_route([])))


def test_nws_hourly_forecast_in_both_schemas():
    periods = [
        {"startTime": "2019-07-01T13:00:00-05:00", "temperature": 90, "temperatureUnit": "F",
         "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 20}},
        {"startTime": "2019-07-01T14:00:00-05:00", "temperature": {"unitCode": "wmoUnit:degC", "value": 30.0},
         "probabilityOfPrecipitation": {"value": None}},
        {"startTime": "2019-07-01T14:00:00-05:00", "temperature": 99, "temperatureUnit": "F"},  # repeated hour
    ]
    session = FakeSession(nws_route(periods))
    nws = NWSHourly(38.0, -97.0, "wellopt-tests", TZ, session=session)
    assert session.headers == {"User-Agent": "wellopt-tests", "Accept": "application/geo+json"}
    w = nws.forecast()
    assert list(w.index) == list(pd.DatetimeIndex(["2019-07-01 13:00", "2019-07-01 14:00"], name="time"))
    assert w["temp_f"].tolist() == pytest.approx([90.0, 86.0])
    assert w["precip_prob"].iloc[0] == pytest.approx(0.2) and pd.isna(w["precip_prob"].iloc[1])
    nws.forecast()
    assert sum(1 for u, *_ in session.calls if "/points/" in u) == 1  # the grid URL is cached


def test_nws_with_no_periods():
    w = NWSHourly(38.0, -97.0, "wellopt-tests", TZ, session=FakeSession(nws_route([]))).forecast()
    assert w.empty and list(w.columns) == ["temp_f", "precip_prob"]
