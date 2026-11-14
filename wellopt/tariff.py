"""Electricity tariff: time-of-use energy prices and monthly demand charges."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import TariffConfig


@dataclass(frozen=True)
class Tariff:
    cfg: TariffConfig

    def is_on_peak(self, index: pd.DatetimeIndex) -> np.ndarray:
        hours = index.hour
        peak = (hours >= self.cfg.on_peak_start_hour) & (hours < self.cfg.on_peak_end_hour)
        if self.cfg.on_peak_weekdays_only:
            peak &= index.day_of_week < 5
        return np.asarray(peak, dtype=bool)

    def energy_price(self, index: pd.DatetimeIndex) -> np.ndarray:
        """$/kWh for each hour."""
        return np.where(self.is_on_peak(index), self.cfg.on_peak_usd_per_kwh, self.cfg.off_peak_usd_per_kwh)

    def demand_hours(self, index: pd.DatetimeIndex) -> np.ndarray:
        """Hours that count toward the billed monthly peak."""
        return self.is_on_peak(index) if self.cfg.demand_charge_on_peak_only else np.ones(len(index), bool)

    def cost(self, kw_by_meter: pd.DataFrame, prior_peak_kw: dict[str, float] | None = None) -> dict:
        """Energy and demand cost for hourly kW per meter (columns = meters).

        Each meter is billed rate x its peak kW in every calendar month the data
        touches. `prior_peak_kw` carries month-to-date peaks set before the window
        starts, so that partial-month windows are billed consistently.
        """
        price = self.energy_price(kw_by_meter.index)
        energy_kwh = float(kw_by_meter.to_numpy().sum())
        energy_usd = float((kw_by_meter.sum(axis=1).to_numpy() * price).sum())
        billable = kw_by_meter[self.demand_hours(kw_by_meter.index)]
        demand_usd = 0.0
        prior_peak_kw = prior_peak_kw or {}
        first_month = kw_by_meter.index[0].to_period("M") if len(kw_by_meter) else None
        for month, block in billable.groupby(billable.index.to_period("M")):
            peaks = block.max()
            if month == first_month:
                peaks = np.maximum(peaks, pd.Series(prior_peak_kw).reindex(peaks.index).fillna(0.0))
            demand_usd += float(peaks.sum()) * self.cfg.demand_charge_usd_per_kw_month
        return {"energy_kwh": energy_kwh, "energy_usd": energy_usd, "demand_usd": demand_usd,
                "total_usd": energy_usd + demand_usd}


def kw_by_meter(on: pd.DataFrame, table: pd.DataFrame) -> pd.DataFrame:
    """Hourly kW per electric meter for an on/off schedule (columns = pumps)."""
    kw = on.fillna(0).mul(table.loc[on.columns, "kw_plan"], axis=1)
    return kw.T.groupby(table.loc[on.columns, "meter"]).sum().T
