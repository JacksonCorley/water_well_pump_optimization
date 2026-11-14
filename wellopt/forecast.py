"""Probabilistic hourly demand forecasts and their rolling-origin evaluation.

Two forecasters share one interface:

* SeasonalNaive: average of "same hour on the latest available day" and "same
  hour last week". Quantiles come from its own historical residuals. This is
  the baseline every other model has to beat.
* QuantileGBM: direct multi-horizon gradient boosting (one model per quantile)
  on lags known at the forecast origin, recent levels, calendar features and
  optional weather. It predicts flow in gpm, or with `target="ratio"` as a
  multiple of the trailing week's mean (the class docstring has the trade-off).
  The last `calib_days` of history are held out and used to recalibrate the
  outer quantiles (split conformal), because raw quantile GBMs give bands that
  are too narrow.

Both forecast h = 1..H hours after an origin T using only data up to and
including T. `evaluate()` checks that claim end to end with a rolling-origin
backtest.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
from sklearn.ensemble import HistGradientBoostingRegressor

from .data import fill_for_model

MIN_HISTORY_HOURS = 24 * 28  # 4 weeks of lags


def qname(q: float) -> str:
    return f"p{int(round(q * 100))}"


def _day_lag(hs: np.ndarray) -> np.ndarray:
    """Lag to the same hour on the most recent day fully known at the origin."""
    return 24 * np.ceil(hs / 24).astype(int)


def _target_times(origin: pd.Timestamp, horizon: int) -> pd.DatetimeIndex:
    return pd.date_range(origin + pd.Timedelta(hours=1), periods=horizon, freq="h")


def _median_q(quantiles) -> float:
    return min(quantiles, key=lambda q: abs(q - 0.5))


def _cum_band(y_obs: np.ndarray, p50: np.ndarray, quantiles) -> dict[str, np.ndarray]:
    """Per lead time h, the outer quantiles of the median forecast's cumulative
    error over hours 1..h (gpm-hours). Storage depends on cumulative demand, and
    adding hourly P90s together would badly overstate the volume at risk.
    `y_obs` and `p50` are (origins, H); NaN actuals drop that origin from later h."""
    cum = np.cumsum(y_obs - p50, axis=1)
    out = {}
    for name, q in (("low", min(quantiles)), ("high", max(quantiles))):
        with np.errstate(all="ignore"):
            band = np.nanquantile(cum, q, axis=0) if np.isfinite(cum).any() else np.zeros(cum.shape[1])
        out[name] = pd.Series(band).ffill().fillna(0.0).to_numpy()
    return out


def _add_scenarios(out: dict, p50: np.ndarray, band: dict | None, H: int) -> None:
    """Hourly demand paths whose running totals follow the cumulative band."""
    if band is None:
        return
    for name in ("low", "high"):
        out[f"scen_{name}"] = p50 + np.diff(np.concatenate([[0.0], band[name][:H]]))


@dataclass
class SeasonalNaive:
    quantiles: tuple[float, ...] = (0.1, 0.5, 0.9)
    horizon: int = 48
    calib_days: int = 180
    name: str = "seasonal_naive"

    def _point(self, arr: np.ndarray, o: np.ndarray, hs: np.ndarray) -> np.ndarray:
        t = o[:, None] + hs[None, :]
        return 0.5 * (arr[t - _day_lag(hs)[None, :]] + arr[t - 168])

    def fit(self, y: pd.Series, valid: pd.Series | None = None, exog=None) -> "SeasonalNaive":
        arr = y.to_numpy(float)
        ok = (valid if valid is not None else y.notna()).to_numpy(bool)
        n, H = len(arr), self.horizon
        hs = np.arange(1, H + 1)
        o = np.arange(max(168, n - self.calib_days * 24 - H), n - H)
        if len(o) == 0:
            raise ValueError("not enough history to calibrate the seasonal-naive quantiles")
        t = o[:, None] + hs[None, :]
        point = self._point(arr, o, hs)
        y_obs = np.where(ok[t], arr[t], np.nan)
        self.offsets_ = {q: np.nanquantile(y_obs - point, q, axis=0) for q in self.quantiles}
        self.cum_band_ = _cum_band(y_obs, point + self.offsets_[_median_q(self.quantiles)], self.quantiles)
        return self

    def predict(self, y: pd.Series, origin: pd.Timestamp, horizon: int | None = None, exog=None) -> pd.DataFrame:
        H = horizon or self.horizon
        arr = y.loc[:origin].to_numpy(float)
        o = np.array([len(arr) - 1])
        hs = np.arange(1, H + 1)
        point = self._point(arr, o, hs)[0]
        out = {qname(q): point + self.offsets_[q][:H] for q in self.quantiles}
        _add_scenarios(out, out[qname(_median_q(self.quantiles))], self.cum_band_, H)
        return _sorted_quantiles(pd.DataFrame(out, index=_target_times(origin, H)), self.quantiles)


def _sorted_quantiles(df: pd.DataFrame, quantiles) -> pd.DataFrame:
    cols = [qname(q) for q in sorted(quantiles)]
    df[cols] = np.sort(df[cols].to_numpy(), axis=1)
    return df.clip(lower=0)


FEATURES = ["h", "hour", "dow", "weekend", "holiday", "doy_sin", "doy_cos",
            "lag_day", "lag_week", "lag_2week", "mean_4w", "last", "level_24", "level_168"]
# With target="ratio", the target and these features are divided by the trailing
# week's mean (level_168, which itself stays in gpm).
RELATIVE = ("lag_day", "lag_week", "lag_2week", "mean_4w", "last", "level_24")
TARGETS = ("absolute", "ratio")


@dataclass
class QuantileGBM:
    """Direct multi-horizon quantile GBM.

    `target="absolute"` (the default) predicts gpm. `target="ratio"` predicts demand as a
    multiple of the trailing week's mean: better average accuracy on this site's data,
    but trees cannot predict past the largest ratio seen in training, so it under-forecast
    a sharp early-summer surge more than the absolute model and the simulated North
    tank nearly emptied in the June 2019 backtest. See docs/METHODOLOGY.md, section 5.
    """
    quantiles: tuple[float, ...] = (0.1, 0.5, 0.9)
    horizon: int = 48
    origin_every: int = 5
    max_iter: int = 300
    learning_rate: float = 0.05
    calib_days: int = 56
    random_state: int = 0
    target: str = "absolute"
    name: str = "quantile_gbm"

    def __post_init__(self):
        if self.target not in TARGETS:
            raise ValueError(f"target must be one of {TARGETS}, not {self.target!r}")

    def _features(self, arr: np.ndarray, start: pd.Timestamp, o: np.ndarray, hs: np.ndarray,
                  exog: pd.DataFrame | None) -> tuple[np.ndarray, list[str], np.ndarray]:
        """Feature matrix with one row per (origin, lead time), and each row's scale: 1, or for
        target="ratio" the trailing week's mean flow, which the target and the RELATIVE features are divided by."""
        t = o[:, None] + hs[None, :]
        cs = np.concatenate([[0.0], np.cumsum(arr)])
        level_24 = (cs[o + 1] - cs[o + 1 - 24]) / 24
        level_168 = (cs[o + 1] - cs[o + 1 - 168]) / 168
        if getattr(self, "target", "absolute") == "ratio":
            scale = np.repeat(np.maximum(level_168, 1.0), len(hs))
        else:
            scale = np.ones(len(o) * len(hs))
        mean_4w = np.mean([arr[t - 168 * k] for k in range(1, 5)], axis=0)
        times = start + pd.to_timedelta(t.ravel(), unit="h")
        hol = USFederalHolidayCalendar().holidays(times.min(), times.max())
        doy = times.day_of_year.to_numpy() / 365.25 * 2 * np.pi
        cols = {
            "h": np.broadcast_to(hs, t.shape).ravel(),
            "hour": times.hour.to_numpy(),
            "dow": times.day_of_week.to_numpy(),
            "weekend": (times.day_of_week >= 5).astype(int),
            "holiday": times.normalize().isin(hol).astype(int),
            "doy_sin": np.sin(doy),
            "doy_cos": np.cos(doy),
            "lag_day": arr[t - _day_lag(hs)[None, :]].ravel(),
            "lag_week": arr[t - 168].ravel(),
            "lag_2week": arr[t - 336].ravel(),
            "mean_4w": mean_4w.ravel(),
            "last": np.repeat(arr[o], len(hs)),
            "level_24": np.repeat(level_24, len(hs)),
            "level_168": np.repeat(level_168, len(hs)),
        }
        for c in RELATIVE:
            cols[c] = cols[c] / scale
        names = list(FEATURES)
        if exog is not None:
            ex = exog.reindex(times)
            for c in ex.columns:
                cols[f"wx_{c}"] = ex[c].to_numpy(float)
                names.append(f"wx_{c}")
        return np.column_stack([cols[c] for c in names]), names, scale

    def fit(self, y: pd.Series, valid: pd.Series | None = None, exog: pd.DataFrame | None = None) -> "QuantileGBM":
        arr = y.to_numpy(float)
        ok = (valid if valid is not None else y.notna()).to_numpy(bool)
        n, H = len(arr), self.horizon
        if n < MIN_HISTORY_HOURS + H + 24:
            raise ValueError("need at least ~4 weeks of history to train QuantileGBM")
        hs = np.arange(1, H + 1)
        calib = self.calib_days * 24
        if n < MIN_HISTORY_HOURS + H + 24 * 28 + calib:
            calib = 0  # too little history to hold out a calibration window
        # Training targets all end before the calibration window starts.
        o = np.arange(MIN_HISTORY_HOURS, n - H - calib, self.origin_every)
        X, names, scale = self._features(arr, y.index[0], o, hs, exog)
        t = (o[:, None] + hs[None, :]).ravel()
        target = arr[t] / scale
        keep = ok[t] & np.isfinite(target)
        self.feature_names_ = names
        self.models_ = {}
        for q in self.quantiles:
            model = HistGradientBoostingRegressor(
                loss="quantile", quantile=q, max_iter=self.max_iter, learning_rate=self.learning_rate,
                max_leaf_nodes=31, min_samples_leaf=50, l2_regularization=1.0,
                early_stopping=True, validation_fraction=0.1, n_iter_no_change=20,
                random_state=self.random_state)
            self.models_[q] = model.fit(X[keep], target[keep])
        self.offsets_ = {q: np.zeros(H) for q in self.quantiles}
        self.cum_band_ = None
        if calib:
            # Split-conformal recalibration: shift each quantile, per lead time, so
            # that it is hit at its nominal rate on the held-out window.
            o_cal = np.arange(n - calib, n - H)
            Xc, _, sc = self._features(arr, y.index[0], o_cal, hs, exog)
            tc = o_cal[:, None] + hs[None, :]
            yc = np.where(ok[tc], arr[tc], np.nan)
            # The median is left as fitted: shifting it toward the last few weeks'
            # bias chases the season (it doubled the South bias in backtests).
            mid = _median_q(self.quantiles)
            preds = {}
            for q, m in self.models_.items():
                preds[q] = (m.predict(Xc) * sc).reshape(len(o_cal), H)
                if q != mid:
                    off = np.nanquantile(yc - preds[q], q, axis=0)
                    self.offsets_[q] = pd.Series(off).rolling(7, center=True, min_periods=1).mean().to_numpy()
            self.cum_band_ = _cum_band(yc, preds[mid], self.quantiles)
        self.calibrated_ = bool(calib)
        self.trained_until_ = y.index[-1]
        return self

    def predict(self, y: pd.Series, origin: pd.Timestamp, horizon: int | None = None,
                exog: pd.DataFrame | None = None) -> pd.DataFrame:
        H = horizon or self.horizon
        hist = y.loc[:origin]
        arr = hist.to_numpy(float)
        if len(arr) <= MIN_HISTORY_HOURS:
            raise ValueError("need at least 4 weeks of history before the origin")
        o = np.array([len(arr) - 1])
        X, _, scale = self._features(arr, hist.index[0], o, np.arange(1, H + 1), exog)
        out = {qname(q): m.predict(X) * scale + self.offsets_[q][:H] for q, m in self.models_.items()}
        _add_scenarios(out, out[qname(_median_q(self.quantiles))], self.cum_band_, H)
        return _sorted_quantiles(pd.DataFrame(out, index=_target_times(origin, H)), self.quantiles)


class PerfectForesight:
    """Oracle 'forecast' that returns the actual demand: an upper bound for backtests."""
    name = "perfect_foresight"

    def __init__(self, actual: pd.Series, quantiles=(0.1, 0.5, 0.9)):
        self.actual, self.quantiles = actual, quantiles

    def fit(self, *args, **kwargs):
        return self

    def predict(self, y, origin, horizon=48, exog=None) -> pd.DataFrame:
        idx = _target_times(origin, horizon)
        vals = self.actual.reindex(idx).ffill().bfill().to_numpy()
        out = {qname(q): vals for q in self.quantiles} | {"scen_low": vals, "scen_high": vals}
        return pd.DataFrame(out, index=idx)


def make_forecaster(kind: str, cfg_forecast, horizon: int):
    if kind == "naive":
        return SeasonalNaive(quantiles=cfg_forecast.quantiles, horizon=horizon)
    if kind == "gbm":
        return QuantileGBM(quantiles=cfg_forecast.quantiles, horizon=horizon,
                           origin_every=cfg_forecast.origin_every_hours,
                           max_iter=cfg_forecast.max_iter, learning_rate=cfg_forecast.learning_rate,
                           calib_days=cfg_forecast.calib_days,
                           target=getattr(cfg_forecast, "target", "absolute"))
    raise ValueError(f"unknown forecaster {kind!r} (use 'naive' or 'gbm')")


# ----------------------------------------------------------------------------- evaluation
BUCKETS = [(1, 6, "1-6h"), (7, 24, "7-24h"), (25, 48, "25-48h")]


def evaluate(y_true: pd.Series, factories: dict, start, end, *, horizon: int = 48,
             origin_every_hours: int = 24, refit_every_days: int = 28, quantiles=(0.1, 0.5, 0.9),
             progress=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rolling-origin backtest.

    At each origin T the series is re-filled from data <= T only, each model is
    (re)fit on data <= T every `refit_every_days`, and h = 1..horizon is
    forecast. Scoring uses only hours whose actual value is a real (not imputed)
    reading.
    """
    origins = pd.date_range(pd.Timestamp(start), pd.Timestamp(end) - pd.Timedelta(hours=horizon),
                            freq=f"{origin_every_hours}h")
    rows = []
    for name, factory in factories.items():
        model, fitted_at = None, None
        t0 = time.time()
        for i, origin in enumerate(origins):
            y_hist = y_true.loc[:origin]
            y_fill, imputed = fill_for_model(y_hist)
            if model is None or (origin - fitted_at) >= pd.Timedelta(days=refit_every_days):
                model = factory().fit(y_fill, valid=~imputed & y_hist.notna())
                fitted_at = origin
            pred = model.predict(y_fill, origin, horizon)
            actual = y_true.reindex(pred.index)
            block = pred.assign(model=name, origin=origin, h=np.arange(1, horizon + 1), actual=actual.to_numpy())
            rows.append(block)
            if progress and i % 30 == 0:
                progress(f"{name}: origin {i + 1}/{len(origins)} ({time.time() - t0:.0f}s)")
    detail = pd.concat(rows).rename_axis("time").reset_index()
    return detail, summarize(detail, quantiles)


def pinball(y: np.ndarray, pred: np.ndarray, q: float) -> np.ndarray:
    d = y - pred
    return np.maximum(q * d, (q - 1) * d)


def summarize(detail: pd.DataFrame, quantiles=(0.1, 0.5, 0.9)) -> pd.DataFrame:
    """Hourly scores per lead-time bucket, plus cumulative-volume scores.

    The `vol_0-24h` and `vol_0-48h` rows score the average flow over the first 24
    or 48 hours after each origin, i.e. the volume, which is what matters for a
    system with storage. Their coverage is that of the scenario band the
    scheduler uses (`scen_low` .. `scen_high`). Origins with any missing actual in
    the window are skipped.
    """
    d = detail.dropna(subset=["actual"])
    lo, mid, hi = qname(min(quantiles)), qname(0.5), qname(max(quantiles))
    out = []
    for model in d["model"].unique():
        dm = d[d["model"] == model]
        for a, b, label in BUCKETS + [(1, 10_000, "all")]:
            s = dm[dm["h"].between(a, b)]
            if s.empty:
                continue
            err = s[mid] - s["actual"]
            out.append({
                "model": model, "horizon": label, "n": len(s),
                "MAE_gpm": err.abs().mean(),
                "MAPE": (err.abs() / s["actual"]).mean(),
                "bias_gpm": err.mean(),
                "pinball": np.mean([pinball(s["actual"].to_numpy(), s[qname(q)].to_numpy(), q).mean()
                                    for q in quantiles]),
                "coverage_p10_p90": ((s["actual"] >= s[lo]) & (s["actual"] <= s[hi])).mean(),
            })
        has_band = {"scen_low", "scen_high"} <= set(dm.columns) and dm["scen_low"].notna().any()
        for label, b in (("vol_0-24h", 24), ("vol_0-48h", 48)):
            s = dm[dm["h"] <= b]
            agg = {"n": ("actual", "size"), "actual": ("actual", "mean"), "pred": (mid, "mean")}
            if has_band:
                agg |= {"lo": ("scen_low", "mean"), "hi": ("scen_high", "mean")}
            g = s.groupby("origin").agg(**agg)
            g = g[g["n"] == b]
            if g.empty:
                continue
            err = g["pred"] - g["actual"]
            cover = ((g["actual"] >= g["lo"]) & (g["actual"] <= g["hi"])).mean() if has_band else np.nan
            out.append({"model": model, "horizon": label, "n": len(g),
                        "MAE_gpm": err.abs().mean(), "MAPE": (err.abs() / g["actual"]).mean(),
                        "bias_gpm": err.mean(), "pinball": np.nan, "coverage_p10_p90": cover})
    return pd.DataFrame(out)
