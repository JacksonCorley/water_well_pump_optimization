"""Data sources for the live runner.

Every source returns hourly rows in the export's format: a naive local wall-clock
DatetimeIndex named "time" and columns such as PMP_1_FLOW, PMP_1_PRESS,
PMP_1_PowerUsage(kW-H) and, when tanks are telemetered, TNK_<Section>_MG.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

import pandas as pd


class CsvReplaySource:
    """Replays a historical export as if it were arriving live.

    A replay clock decides what 'now' is, and `fetch` never returns rows at or after
    it, so the runner cannot see the future.
    """
    name = "csv-replay"

    def __init__(self, raw: pd.DataFrame, start=None, warmup_days: int = 35):
        self.raw = raw.sort_index()
        earliest = (self.raw.index.min() + pd.Timedelta(days=warmup_days)).ceil("h")
        clock = pd.Timestamp(start) if start is not None else self.raw.index.max() - pd.Timedelta(days=7)
        self.clock = max(clock.floor("h"), earliest)

    @property
    def exhausted(self) -> bool:
        return self.clock > self.raw.index.max() + pd.Timedelta(hours=1)

    def now(self) -> pd.Timestamp:
        return self.clock

    def advance(self, hours: int = 1) -> None:
        self.clock += pd.Timedelta(hours=hours)

    def fetch(self, start, end) -> pd.DataFrame:
        """Rows with start <= time < min(end, now)."""
        end = min(pd.Timestamp(end), self.clock)
        return self.raw.loc[(self.raw.index >= pd.Timestamp(start)) & (self.raw.index < end)]


def load_tag_map(path: str | Path) -> dict[str, str]:
    """Two-column CSV (column,pi_path) mapping export column names to PI point paths."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("column") and r.get("pi_path")]
    return {r["column"].strip(): r["pi_path"].strip() for r in rows}


class PIWebAPISource:
    """Read-only adapter for an OSIsoft/AVEVA PI Web API historian (a starting point).

    * `tag_map` maps export column names (PMP_1_FLOW, ...) to PI point paths
      (\\\\PISERVER\\WELL01.FLOW). Load it with `load_tag_map`.
    * Values are hourly time-weighted averages from the `summary` endpoint, which
      matches an hourly export. Each value is stamped with the start of its hour
      (`timeType=EarliestTime`); change `time_type` if the export is hour-ending.
    * Timestamps arrive in UTC and are converted to naive local wall-clock time,
      the convention QC expects (the repeated autumn hour keeps its first value).
    * Credentials come from the environment (WELLOPT_PI_USER, WELLOPT_PI_PASSWORD),
      never from the config file. Kerberos sites can pass a requests auth object.
    * Only GET requests are made: the adapter cannot write to the historian.
    """
    name = "pi"

    def __init__(self, base_url: str, tag_map: dict[str, str], timezone: str, *, auth=None,
                 verify: bool | str = True, session=None, timeout: float = 30.0, time_type: str = "EarliestTime"):
        if not base_url:
            raise ValueError("set [live] pi_base_url in the site config to use the PI source")
        if not tag_map:
            raise ValueError("the PI tag map is empty")
        if session is None:
            import requests
            session = requests.Session()
        if auth is None and os.environ.get("WELLOPT_PI_USER"):
            auth = (os.environ["WELLOPT_PI_USER"], os.environ.get("WELLOPT_PI_PASSWORD", ""))
        session.auth = auth
        session.verify = verify
        session.headers.update({"Accept": "application/json"})
        self.base_url = base_url.rstrip("/")
        self.tag_map = dict(tag_map)
        self.tz = timezone
        self.session = session
        self.timeout = timeout
        self.time_type = time_type
        self._web_ids: dict[str, str] = {}

    def now(self) -> pd.Timestamp:
        return pd.Timestamp.now(tz=self.tz).tz_localize(None).floor("h")

    def _get(self, url: str, params: dict | None = None) -> dict:
        r = self.session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def web_id(self, path: str) -> str:
        if path not in self._web_ids:
            self._web_ids[path] = self._get(f"{self.base_url}/points", {"path": path})["WebId"]
        return self._web_ids[path]

    def _utc(self, t) -> str:
        ts = pd.Timestamp(t).tz_localize(self.tz, ambiguous=True, nonexistent="shift_forward")
        return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")

    def fetch_series(self, path: str, start, end) -> pd.Series:
        data = self._get(f"{self.base_url}/streams/{self.web_id(path)}/summary",
                         {"startTime": self._utc(start), "endTime": self._utc(end), "summaryType": "Average",
                          "summaryDuration": "1h", "calculationBasis": "TimeWeighted", "timeType": self.time_type})
        times, values = [], []
        for item in data.get("Items", []):
            v = item.get("Value") or {}
            val = v.get("Value")
            if isinstance(val, dict) or not v.get("Good", True):  # digital state or bad quality
                val = None
            times.append(v.get("Timestamp"))
            values.append(val)
        idx = pd.to_datetime(pd.Series(times, dtype=object), utc=True)
        idx = pd.DatetimeIndex(idx).tz_convert(self.tz).tz_localize(None)
        s = pd.Series(pd.to_numeric(pd.Series(values, dtype=object), errors="coerce").to_numpy(float), index=idx)
        return s[~s.index.duplicated(keep="first")]

    def fetch(self, start, end) -> pd.DataFrame:
        """Hourly rows with start <= time < min(end, now)."""
        start, end = pd.Timestamp(start), min(pd.Timestamp(end), self.now())
        if end <= start:
            return pd.DataFrame(columns=list(self.tag_map), index=pd.DatetimeIndex([], name="time"))
        df = pd.DataFrame({col: self.fetch_series(path, start, end) for col, path in self.tag_map.items()})
        df = df.sort_index()
        df = df[(df.index >= start) & (df.index < end)]
        df.index.name = "time"
        return df


def make_pi_source(cfg) -> PIWebAPISource:
    live = cfg.live
    verify: bool | str = live.pi_ca_bundle or live.pi_verify_tls
    return PIWebAPISource(live.pi_base_url, load_tag_map(cfg.path(live.pi_tag_map)), cfg.timezone, verify=verify)
