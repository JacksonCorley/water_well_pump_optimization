"""Saved demand models: reuse, retraining and safe saving (wellopt.live.models)."""
from __future__ import annotations

from dataclasses import replace

import joblib
import pandas as pd
import pytest

from tests import synthetic as syn
from wellopt.data import fill_for_model
from wellopt.live.models import demand_series, get_forecaster, load, model_path

APRIL = pd.Timestamp("2019-04-01")
MARCH = pd.Timestamp("2019-03-01")


@pytest.fixture
def mctx(ctx, tmp_path):
    return syn.ctx_in(ctx, tmp_path)


def saved_until(c, kind="naive"):
    return pd.Timestamp(joblib.load(model_path(c, "North", kind))["meta"]["trained_until"])


def test_gbm_is_trained_once_then_loaded(mctx):
    model, meta, how = get_forecaster(mctx, "North", "gbm", APRIL)
    assert how == "trained"
    assert meta["trained_until"] == APRIL - syn.H1  # only data before `now`
    assert model_path(mctx, "North", "gbm").exists()
    again, meta2, how2 = get_forecaster(mctx, "North", "gbm", APRIL + 5 * syn.H1)
    assert how2 == "loaded" and meta2["trained_until"] == meta["trained_until"]
    fill, _ = fill_for_model(demand_series(mctx, "North").loc[: APRIL - syn.H1])
    pd.testing.assert_frame_equal(model.predict(fill, APRIL - syn.H1, 24), again.predict(fill, APRIL - syn.H1, 24))


def test_old_or_mismatched_models_are_retrained(mctx):
    get_forecaster(mctx, "North", "naive", MARCH)
    assert get_forecaster(mctx, "North", "naive", APRIL)[2] == "trained"  # 31 days old > retrain_every_days 28
    assert get_forecaster(mctx, "North", "naive", APRIL + 24 * syn.H1, max_age_days=40)[2] == "loaded"
    other = replace(mctx, cfg=replace(mctx.cfg, forecast=replace(mctx.cfg.forecast, calib_days=7)))
    assert get_forecaster(other, "North", "naive", APRIL + 24 * syn.H1, max_age_days=40)[2] == "trained"


def test_a_model_trained_on_later_data_is_never_used_for_an_earlier_hour(mctx):
    get_forecaster(mctx, "North", "naive", APRIL)
    model, meta, how = get_forecaster(mctx, "North", "naive", MARCH)
    assert how == "trained" and meta["trained_until"] == MARCH - syn.H1
    assert saved_until(mctx) == APRIL - syn.H1  # the newer saved model was kept


def test_save_new_false_writes_nothing(mctx):
    _, _, how = get_forecaster(mctx, "North", "naive", APRIL, save_new=False)
    assert how == "trained"
    assert not model_path(mctx, "North", "naive").exists()


def test_a_corrupt_model_file_is_replaced(mctx):
    path = model_path(mctx, "North", "naive")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a model")
    assert load(mctx, "North", "naive") is None
    assert get_forecaster(mctx, "North", "naive", APRIL)[2] == "trained"
    assert saved_until(mctx) == APRIL - syn.H1
    assert not path.with_suffix(".tmp").exists()
