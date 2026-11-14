"""Demand forecasters and the rolling-origin evaluation (wellopt.forecast)."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from tests import synthetic as syn
from wellopt.forecast import PerfectForesight, QuantileGBM, evaluate, make_forecaster

ORIGIN = syn.PLAN_AT - syn.H1
QCOLS = ["p10", "p50", "p90"]


@pytest.fixture(scope="module")
def demand(ctx) -> pd.Series:
    return ctx.qc.production_filled["North"]


@pytest.fixture(scope="module", params=["naive", "gbm", "gbm-ratio"])
def fitted(request, ctx, demand):
    cfg = ctx.cfg.forecast if request.param != "gbm-ratio" else replace(ctx.cfg.forecast, target="ratio")
    model = make_forecaster(request.param.split("-")[0], cfg, 24)
    return model.fit(demand.loc[:ORIGIN])


def test_forecast_covers_the_next_hours_with_ordered_quantiles(fitted, demand):
    out = fitted.predict(demand.loc[:ORIGIN], ORIGIN)
    assert list(out.index) == list(pd.date_range(syn.PLAN_AT, periods=24, freq="h"))
    assert set(QCOLS) <= set(out.columns)
    q = out[QCOLS].to_numpy()
    assert np.isfinite(q).all()
    assert (np.diff(q, axis=1) >= -1e-9).all()
    assert (q >= 0).all()
    # The synthetic demand runs at 30-60% of North's 3700 gpm; the median should be in that range.
    assert 0.2 * 3700 < out["p50"].mean() < 0.8 * 3700


def test_forecast_ignores_data_after_the_origin(fitted, demand):
    future_tripled = demand.copy()
    future_tripled[future_tripled.index > ORIGIN] *= 3
    pd.testing.assert_frame_equal(fitted.predict(demand, ORIGIN), fitted.predict(future_tripled, ORIGIN))


def test_gbm_has_calibrated_scenarios(ctx, demand):
    model = make_forecaster("gbm", ctx.cfg.forecast, 24).fit(demand.loc[:ORIGIN])
    assert model.calibrated_
    out = model.predict(demand.loc[:ORIGIN], ORIGIN)
    assert {"scen_low", "scen_high"} <= set(out.columns)
    assert out["scen_high"].sum() >= out["p50"].sum() >= out["scen_low"].sum()


def test_gbm_needs_four_weeks_of_history(ctx, demand):
    with pytest.raises(ValueError):
        make_forecaster("gbm", ctx.cfg.forecast, 24).fit(demand.iloc[: 20 * 24])


def test_unknown_forecaster_or_target_is_rejected(ctx):
    with pytest.raises(ValueError):
        make_forecaster("crystal_ball", ctx.cfg.forecast, 24)
    with pytest.raises(ValueError):
        QuantileGBM(target="log")


def test_perfect_foresight_returns_the_actuals(demand):
    pf = PerfectForesight(demand)
    out = pf.predict(None, ORIGIN, 24)
    np.testing.assert_allclose(out["p50"], demand.loc[syn.PLAN_AT: syn.PLAN_AT + 23 * syn.H1])
    np.testing.assert_allclose(out["p10"], out["p90"])
    tail = pf.predict(None, syn.DATA_END - 5 * syn.H1, 24)  # runs past the data: last value repeats
    assert len(tail) == 24
    assert tail["p50"].iloc[-1] == demand.iloc[-1]


def test_evaluate_uses_only_data_before_each_origin(ctx, demand):
    start, end = pd.Timestamp("2019-03-01"), pd.Timestamp("2019-03-08")
    change_at = pd.Timestamp("2019-03-05 12:00")
    changed = demand.copy()
    changed[changed.index >= change_at] += 800
    factories = {"naive": lambda: make_forecaster("naive", ctx.cfg.forecast, 24)}
    kw = dict(horizon=24, origin_every_hours=24, refit_every_days=28)
    d1, summary = evaluate(demand, factories, start, end, **kw)
    d2, _ = evaluate(changed, factories, start, end, **kw)
    assert d1["origin"].nunique() == 7
    assert len(d1) == 7 * 24
    assert set(summary["model"]) == {"naive"}
    for origin, g1 in d1.groupby("origin"):
        g2 = d2[d2["origin"] == origin]
        same = np.allclose(g1[QCOLS].to_numpy(), g2[QCOLS].to_numpy())
        assert same == (origin < change_at), origin
