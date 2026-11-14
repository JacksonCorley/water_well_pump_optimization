"""Train, save and load the demand forecasters used by the live runner.

Models are saved with joblib (a pickle). Only load model files this project wrote:
unpickling a file from an untrusted source can run arbitrary code.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import joblib
import pandas as pd

from .. import __version__
from ..data import fill_for_model, reconstruct_demand
from ..forecast import make_forecaster


def demand_series(ctx, section: str) -> pd.Series:
    """Demand to forecast: production corrected for tank level change when tanks are telemetered."""
    tank = ctx.qc.tank_mg
    tank = tank[section] if tank is not None and section in tank else None
    return reconstruct_demand(ctx.qc.production[section], tank)


def model_path(ctx, section: str, kind: str) -> Path:
    return ctx.models_dir() / f"forecaster_{section}_{kind}.joblib"


def _signature(ctx, kind: str) -> dict:
    """Settings a saved model must match to be reused."""
    return {"kind": kind, "horizon": ctx.cfg.ops.horizon_hours, "forecast": dataclasses.asdict(ctx.cfg.forecast),
            "version": __version__}


def train(ctx, section: str, kind: str = "gbm", until=None):
    """Fit a forecaster on the section's demand up to `until` (inclusive). Returns (model, meta)."""
    y = demand_series(ctx, section)
    if until is not None:
        y = y.loc[:pd.Timestamp(until)]
    first = y.first_valid_index()
    if first is None:
        raise ValueError(f"no valid {section} demand to train on")
    y = y.loc[first:]
    fill, imputed = fill_for_model(y)
    model = make_forecaster(kind, ctx.cfg.forecast, ctx.cfg.ops.horizon_hours).fit(fill, valid=~imputed & y.notna())
    meta = {"section": section, "trained_until": pd.Timestamp(y.index[-1]), "n_hours": int(len(y)),
            "trained_at": datetime.now().isoformat(timespec="seconds"), **_signature(ctx, kind)}
    return model, meta


def save(ctx, section: str, kind: str, model, meta: dict) -> Path:
    path = model_path(ctx, section, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    joblib.dump({"model": model, "meta": meta}, tmp)
    tmp.replace(path)  # atomic, so the runner never reads a half-written file
    return path


def load(ctx, section: str, kind: str):
    path = model_path(ctx, section, kind)
    if not path.exists():
        return None
    try:
        obj = joblib.load(path)
        return obj["model"], obj["meta"]
    except Exception:  # corrupt or written by an incompatible version: retrain
        return None


def train_and_save(ctx, section: str, kind: str = "gbm") -> Path:
    model, meta = train(ctx, section, kind)
    return save(ctx, section, kind, model, meta)


def get_forecaster(ctx, section: str, kind: str, now, *, max_age_days: int | None = None, save_new: bool = True,
                   log=None):
    """A forecaster trained only on data before `now`, at most `max_age_days` old.

    Reuses the saved model when it qualifies, otherwise trains one on the data
    before `now`. A new model is saved only if it is at least as recent as the
    saved one, so planning a past hour never replaces a newer model.
    Returns (model, meta, "loaded" | "trained").
    """
    origin = pd.Timestamp(now) - pd.Timedelta(hours=1)
    max_age = pd.Timedelta(days=max_age_days or ctx.cfg.live.retrain_every_days)
    loaded = load(ctx, section, kind)
    if loaded is not None:
        model, meta = loaded
        until = pd.Timestamp(meta["trained_until"])
        same = all(meta.get(k) == v for k, v in _signature(ctx, kind).items())
        if same and until <= origin and origin - until <= max_age:
            return model, meta, "loaded"
    if log:
        log(f"training the {kind} forecaster for {section} on data through {origin:%Y-%m-%d %H:%M}")
    model, meta = train(ctx, section, kind, until=origin)
    if save_new and (loaded is None or pd.Timestamp(loaded[1]["trained_until"]) <= meta["trained_until"]):
        save(ctx, section, kind, model, meta)
    return model, meta, "trained"
