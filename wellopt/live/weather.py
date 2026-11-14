"""Hourly weather forecast from the US National Weather Service (api.weather.gov).

Optional. The runner stores it so operators see the next two days' temperatures
next to the plan. The demand forecasters do not use it yet: a temperature-aware
model needs historical weather for the same hours as the SCADA history (for
example NOAA hourly observations from the nearest station), passed to
`QuantileGBM.fit(..., exog=...)` and `predict(..., exog=...)` as columns.
"""
from __future__ import annotations

import pandas as pd

NWS_API = "https://api.weather.gov"


class NWSHourly:
    def __init__(self, lat: float, lon: float, user_agent: str, timezone: str, *, session=None,
                 timeout: float = 15.0):
        if not user_agent:
            raise ValueError("the NWS API requires a User-Agent that identifies the application")
        if session is None:
            import requests
            session = requests.Session()
        # NWS asks for a User-Agent it can use to reach the operator of the app.
        # Set [live] weather_user_agent; nothing personal is added here.
        session.headers.update({"User-Agent": user_agent, "Accept": "application/geo+json"})
        self.lat, self.lon, self.tz = float(lat), float(lon), timezone
        self.session, self.timeout = session, timeout
        self._hourly_url: str | None = None

    def _get(self, url: str) -> dict:
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def hourly_url(self) -> str:
        if self._hourly_url is None:
            props = self._get(f"{NWS_API}/points/{self.lat:.4f},{self.lon:.4f}")["properties"]
            self._hourly_url = props["forecastHourly"]
        return self._hourly_url

    def forecast(self) -> pd.DataFrame:
        """Hourly temp_f and precip_prob (0-1), indexed by naive local time."""
        rows = []
        for p in self._get(self.hourly_url())["properties"]["periods"]:
            temp = p.get("temperature")
            if isinstance(temp, dict):  # newer schema: {"unitCode": "wmoUnit:degC", "value": 21.1}
                unit = "C" if "degC" in str(temp.get("unitCode", "")) else "F"
                temp = temp.get("value")
            else:
                unit = p.get("temperatureUnit", "F")
            if temp is not None and unit == "C":
                temp = temp * 9 / 5 + 32
            pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
            t = pd.Timestamp(p["startTime"]).tz_convert(self.tz).tz_localize(None)
            rows.append({"time": t, "temp_f": temp, "precip_prob": None if pop is None else pop / 100})
        if not rows:
            return pd.DataFrame(columns=["temp_f", "precip_prob"], index=pd.DatetimeIndex([], name="time"))
        return pd.DataFrame(rows).drop_duplicates("time").set_index("time").astype(float)
