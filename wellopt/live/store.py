"""SQLite store shared by the live runner (the only writer) and the app (readers).

Tables: runs (one row per planning cycle), plans (one row per section per run),
alerts, hourly (cached historian rows) and weather. WAL mode lets the app read
while the runner writes.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ..scheduler import SectionPlan

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,       -- wall-clock time the cycle ran
    plan_time TEXT,                 -- the hour planned from (simulated during a replay)
    data_end TEXT,                  -- last hour of data the cycle saw
    source TEXT,
    status TEXT NOT NULL,           -- running | ok | partial | stale | error | interrupted
    message TEXT,
    duration_s REAL
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id),
    created_at TEXT NOT NULL,
    plan_start TEXT NOT NULL,
    section TEXT NOT NULL,
    status TEXT,
    summary TEXT NOT NULL,          -- JSON
    record TEXT NOT NULL            -- JSON from SectionPlan.to_record()
);
CREATE INDEX IF NOT EXISTS plans_by_section ON plans(section, id);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    plan_time TEXT,
    section TEXT,
    level TEXT NOT NULL,            -- info | warning | alert
    key TEXT NOT NULL,              -- what the alert is about, used to avoid repeats
    message TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_by_key ON alerts(key, section, plan_time);
CREATE TABLE IF NOT EXISTS hourly (
    time TEXT PRIMARY KEY,
    data TEXT NOT NULL              -- JSON object: one export row
);
CREATE TABLE IF NOT EXISTS weather (
    time TEXT PRIMARY KEY,
    temp_f REAL,
    precip_prob REAL,
    fetched_at TEXT
);
"""


def _iso(t) -> str | None:
    return None if t is None else str(pd.Timestamp(t))


def _now() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def _clean(obj):
    """Make an object strict-JSON safe (NaN -> null, numpy -> Python)."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer, np.bool_)):
        return obj.item()
    if isinstance(obj, (float, np.floating)):
        return None if math.isnan(obj) or math.isinf(obj) else float(obj)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return str(obj)
    return obj


def _dumps(obj) -> str:
    return json.dumps(_clean(obj), allow_nan=False)


class PlanStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            yield con
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------ runs
    def start_run(self, plan_time, source: str) -> int:
        with self._connect() as con:
            # A cycle takes seconds; one still "running" an hour after it started died with its process.
            cutoff = (datetime.now() - timedelta(hours=1)).isoformat(sep=" ", timespec="seconds")
            con.execute("UPDATE runs SET status='interrupted', message='the planner stopped before this run "
                        "finished' WHERE status='running' AND started_at < ?", (cutoff,))
            cur = con.execute("INSERT INTO runs(started_at, plan_time, source, status) VALUES (?, ?, ?, 'running')",
                              (_now(), _iso(plan_time), source))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, message: str = "", data_end=None,
                   duration_s: float | None = None) -> None:
        with self._connect() as con:
            con.execute("UPDATE runs SET status=?, message=?, data_end=?, duration_s=? WHERE id=?",
                        (status, message, _iso(data_end), duration_s, run_id))

    def recent_runs(self, limit: int = 50) -> pd.DataFrame:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    # ----------------------------------------------------------------- plans
    def save_plans(self, plans: dict[str, SectionPlan], created_at=None, data_end=None, run_id: int | None = None,
                   source: str = "cli") -> int:
        if run_id is None:
            first = next(iter(plans.values()), None)
            run_id = self.start_run(first.timeline.index[0] if first is not None else None, source)
            self.finish_run(run_id, "ok", f"{len(plans)} plan(s) saved", data_end)
        created = _iso(created_at) if created_at is not None else _now()
        with self._connect() as con:
            for sec, plan in plans.items():
                con.execute("INSERT INTO plans(run_id, created_at, plan_start, section, status, summary, record) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (run_id, created, str(plan.timeline.index[0]), sec, plan.status,
                             _dumps(plan.summary), _dumps(plan.to_record())))
        return run_id

    def latest_plans(self) -> dict[str, SectionPlan]:
        """Newest plan per section, with created_at, run_id and plan_id added to the summary."""
        with self._connect() as con:
            rows = con.execute("SELECT p.* FROM plans p JOIN (SELECT section, MAX(id) AS id FROM plans "
                               "GROUP BY section) m ON p.id = m.id ORDER BY p.section").fetchall()
        out = {}
        for r in rows:
            plan = SectionPlan.from_record(json.loads(r["record"]))
            plan.summary.update({"created_at": r["created_at"], "run_id": r["run_id"], "plan_id": r["id"]})
            out[r["section"]] = plan
        return out

    def plan_history(self, section: str | None = None, limit: int = 500) -> pd.DataFrame:
        """One row per stored plan: when it was made, for which hour, and its summary."""
        sql = "SELECT id, run_id, created_at, plan_start, section, status, summary FROM plans"
        args: tuple = ()
        if section:
            sql, args = sql + " WHERE section=?", (section,)
        with self._connect() as con:
            rows = con.execute(sql + " ORDER BY id DESC LIMIT ?", args + (limit,)).fetchall()
        recs = []
        for r in rows:
            s = json.loads(r["summary"])
            s.update({k: r[k] for k in ("id", "run_id", "created_at", "plan_start", "section", "status")})
            recs.append(s)
        return pd.DataFrame(recs)

    # ---------------------------------------------------------------- alerts
    def add_alert(self, section: str | None, level: str, key: str, message: str, plan_time=None,
                  dedupe_hours: float = 6) -> dict:
        """Record an alert unless the same unacknowledged alert was raised within `dedupe_hours`."""
        plan_time = _iso(plan_time) or _now()
        alert = {"section": section, "level": level, "key": key, "message": message, "plan_time": plan_time}
        with self._connect() as con:
            if dedupe_hours:
                since = str(pd.Timestamp(plan_time) - pd.Timedelta(hours=dedupe_hours))
                row = con.execute("SELECT id FROM alerts WHERE key=? AND IFNULL(section, '')=IFNULL(?, '') "
                                  "AND acknowledged=0 AND plan_time>=? AND plan_time<=? ORDER BY id DESC LIMIT 1",
                                  (key, section, since, plan_time)).fetchone()
                if row is not None:
                    return alert | {"id": int(row["id"]), "new": False}
            cur = con.execute("INSERT INTO alerts(created_at, plan_time, section, level, key, message) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (_now(), plan_time, section, level, key, message))
            return alert | {"id": int(cur.lastrowid), "new": True}

    def recent_alerts(self, limit: int = 50, include_acknowledged: bool = True) -> pd.DataFrame:
        where = "" if include_acknowledged else " WHERE acknowledged=0"
        with self._connect() as con:
            rows = con.execute(f"SELECT * FROM alerts{where} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    def acknowledge(self, alert_id: int) -> None:
        with self._connect() as con:
            con.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (int(alert_id),))

    # ------------------------------------------------------------- retention
    def prune(self, plans_before=None, hourly_before=None) -> dict[str, int]:
        """Delete plans that start before `plans_before` (the newest plan per section is
        always kept), runs left without plans, and cached historian rows before `hourly_before`."""
        out = {"plans": 0, "runs": 0, "hourly": 0}
        with self._connect() as con:
            if plans_before is not None:
                cut = _iso(plans_before)
                out["plans"] = con.execute(
                    "DELETE FROM plans WHERE plan_start < ? AND id NOT IN "
                    "(SELECT MAX(id) FROM plans GROUP BY section)", (cut,)).rowcount
                out["runs"] = con.execute(
                    "DELETE FROM runs WHERE plan_time < ? AND id NOT IN "
                    "(SELECT run_id FROM plans WHERE run_id IS NOT NULL)", (cut,)).rowcount
            if hourly_before is not None:
                out["hourly"] = con.execute("DELETE FROM hourly WHERE time < ?", (_iso(hourly_before),)).rowcount
        return out

    # ---------------------------------------------------------- hourly cache
    def upsert_hourly(self, df: pd.DataFrame) -> int:
        rows = [(str(t), _dumps({k: (None if pd.isna(v) else float(v)) for k, v in row.items()}))
                for t, row in df.iterrows()]
        with self._connect() as con:
            con.executemany("INSERT INTO hourly(time, data) VALUES (?, ?) "
                            "ON CONFLICT(time) DO UPDATE SET data=excluded.data", rows)
        return len(rows)

    def load_hourly(self, start=None, end=None) -> pd.DataFrame:
        lo = _iso(start) or "0000"
        hi = _iso(end) or "9999"
        with self._connect() as con:
            rows = con.execute("SELECT time, data FROM hourly WHERE time>=? AND time<? ORDER BY time",
                               (lo, hi)).fetchall()
        if not rows:
            return pd.DataFrame(index=pd.DatetimeIndex([], name="time"))
        df = pd.DataFrame([json.loads(r["data"]) for r in rows],
                          index=pd.DatetimeIndex(pd.to_datetime([r["time"] for r in rows]), name="time"))
        return df.astype(float)

    def last_hourly_time(self) -> pd.Timestamp | None:
        with self._connect() as con:
            row = con.execute("SELECT MAX(time) AS t FROM hourly").fetchone()
        return pd.Timestamp(row["t"]) if row and row["t"] else None

    # --------------------------------------------------------------- weather
    def save_weather(self, df: pd.DataFrame) -> None:
        fetched = _now()
        rows = [(str(t), None if pd.isna(r.get("temp_f")) else float(r["temp_f"]),
                 None if pd.isna(r.get("precip_prob")) else float(r["precip_prob"]), fetched)
                for t, r in df.iterrows()]
        with self._connect() as con:
            con.executemany("INSERT INTO weather(time, temp_f, precip_prob, fetched_at) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(time) DO UPDATE SET temp_f=excluded.temp_f, "
                            "precip_prob=excluded.precip_prob, fetched_at=excluded.fetched_at", rows)

    def load_weather(self, start=None, end=None) -> pd.DataFrame:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM weather WHERE time>=? AND time<? ORDER BY time",
                               (_iso(start) or "0000", _iso(end) or "9999")).fetchall()
        if not rows:
            return pd.DataFrame(columns=["temp_f", "precip_prob"], index=pd.DatetimeIndex([], name="time"))
        df = pd.DataFrame([dict(r) for r in rows])
        return df.set_index(pd.DatetimeIndex(pd.to_datetime(df.pop("time")), name="time"))
