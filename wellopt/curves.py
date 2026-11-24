"""Pump curves and operating envelopes, for the ops page.

A pump's operating point is its flow and the total dynamic head it works against. Head is not
measured: it is estimated from the spec sheet's static level and specific capacity plus the
discharge pressure (pumps.total_dynamic_head). A point can sit off a true curve just because those
numbers are out of date.

There are two kinds of curve:
* maker: config/pump_curves.csv, filled in by the utility from the manufacturer's curve or a pump
  test, one row per point. When it includes efficiencies, the best-efficiency point (BEP) is known,
  so the page can show the preferred operating region around it and how far a pump runs from it.
* fitted: H = H0 + k Q^2 fitted to the pump's own running hours. It is shown only when it falls
  with flow and fits well. Most pumps in the 2016-2019 data run at nearly one point, so there is no
  curve to fit. A few trace head rising with flow, which a fixed-speed pump cannot do; the likely
  causes are a VFD, or a wrong static level or specific capacity.

Without either curve, the page shows the pump's history and usual range only.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import CurvesConfig

QUANTILES = (0.01, 0.05, 0.5, 0.95, 0.99)
CURVE_COLUMNS = ("pump", "flow_gpm", "head_ft", "efficiency_pct", "source")
MIN_POINTS = 3          # points needed for a maker curve
MIN_FIT_HOURS = 50      # running hours needed to fit a curve to SCADA
MIN_ENVELOPE_HOURS = 24
RISING_MIN_R2 = 0.3     # a fit that rises with flow at least this well is reported as rising


@dataclass
class PumpCurve:
    """Head (and optionally efficiency) against flow, flow increasing."""
    pump: str
    kind: str                                  # "maker" or "fitted"
    flow_gpm: np.ndarray
    head_ft: np.ndarray
    efficiency_pct: np.ndarray | None = None
    source: str = ""
    r2: float | None = None                    # fitted curves only

    def head_at(self, q):
        """Head on the curve at flow `q`, NaN outside the curve's flow range."""
        q = np.asarray(q, dtype=float)
        h = np.interp(q, self.flow_gpm, self.head_ft)
        return np.where((q >= self.flow_gpm[0]) & (q <= self.flow_gpm[-1]), h, np.nan)[()]

    @property
    def bep_gpm(self) -> float | None:
        """Best-efficiency flow: the listed point with the highest efficiency, refined by a parabola
        through it and its two neighbours. None without efficiencies."""
        if self.efficiency_pct is None:
            return None
        ok = np.isfinite(self.efficiency_pct)
        if not ok.any():
            return None
        q, e = self.flow_gpm[ok], self.efficiency_pct[ok]
        i = int(np.argmax(e))
        if 0 < i < len(q) - 1:
            a, b, _ = np.polyfit(q[i - 1:i + 2], e[i - 1:i + 2], 2)
            if a < 0 and q[i - 1] <= -b / (2 * a) <= q[i + 1]:
                return float(-b / (2 * a))
        return float(q[i])


@dataclass
class FieldFit:
    """H = h0 + k Q^2 fitted to a pump's running hours."""
    h0: float
    k: float
    r2: float
    hours: int
    flow_lo: float      # 1st and 99th percentile of the fitted flows: the range the fit speaks for
    flow_hi: float

    def credible(self, min_r2: float) -> bool:
        """Falls with flow, as a pump curve does, and explains enough of the scatter."""
        return self.k < 0 and np.isfinite(self.r2) and self.r2 >= min_r2

    @property
    def rises(self) -> bool:
        """Head clearly rises with flow, which a fixed-speed pump's curve cannot do."""
        return self.k > 0 and np.isfinite(self.r2) and self.r2 >= RISING_MIN_R2

    def curve(self, pump: str, points: int = 60) -> PumpCurve:
        q = np.linspace(self.flow_lo, self.flow_hi, points)
        return PumpCurve(pump, "fitted", q, self.h0 + self.k * q ** 2, None,
                         f"fitted to {self.hours:,} running hours", self.r2)


def fit_field_curve(q, h) -> FieldFit | None:
    """Least-squares H = h0 + k Q^2 over running hours; None with fewer than MIN_FIT_HOURS."""
    q, h = np.asarray(q, dtype=float), np.asarray(h, dtype=float)
    ok = np.isfinite(q) & np.isfinite(h)
    q, h = q[ok], h[ok]
    if len(q) < MIN_FIT_HOURS:
        return None
    A = np.c_[np.ones_like(q), q ** 2]
    (h0, k), *_ = np.linalg.lstsq(A, h, rcond=None)
    ss = float(((h - h.mean()) ** 2).sum())
    r2 = 1 - float(((h - A @ np.array([h0, k])) ** 2).sum()) / ss if ss > 0 else float("nan")
    lo, hi = np.quantile(q, [0.01, 0.99])
    return FieldFit(float(h0), float(k), r2, int(len(q)), float(lo), float(hi))


@dataclass
class Envelope:
    """Where a pump has run: flow and head quantiles over its running hours, and a sample of them."""
    hours: int
    since: pd.Timestamp
    flow: dict[float, float]        # quantile -> gpm
    head: dict[float, float]        # quantile -> ft
    sample_flow: np.ndarray
    sample_head: np.ndarray

    def usual(self) -> tuple[float, float, float, float]:
        """The usual range: 5th to 95th percentile of flow and of head."""
        return self.flow[0.05], self.flow[0.95], self.head[0.05], self.head[0.95]


def envelope(q: pd.Series, h: pd.Series, sample: int = 300, seed: int = 0) -> Envelope | None:
    """Quantiles of flow and head over running hours, and a fixed random sample of hours inside the
    1st-99th percentile box (for the chart). None with fewer than MIN_ENVELOPE_HOURS."""
    ok = q.notna() & h.notna()
    q, h = q[ok], h[ok]
    if len(q) < MIN_ENVELOPE_HOURS:
        return None
    fq, fh = q.quantile(QUANTILES), h.quantile(QUANTILES)
    inside = (q.between(fq.loc[0.01], fq.loc[0.99]) & h.between(fh.loc[0.01], fh.loc[0.99])).to_numpy()
    idx = np.flatnonzero(inside) if inside.any() else np.arange(len(q))
    pick = np.sort(np.random.default_rng(seed).choice(idx, size=min(sample, len(idx)), replace=False))
    return Envelope(int(len(q)), pd.Timestamp(q.index[0]), {float(k): float(v) for k, v in fq.items()},
                    {float(k): float(v) for k, v in fh.items()}, q.to_numpy(float)[pick], h.to_numpy(float)[pick])


# ----------------------------------------------------------------------------- checks
@dataclass
class PointCheck:
    label: str                          # short, for the pump list: "Usual", "Flow high", "62% of BEP", ...
    level: str                          # good | warning
    notes: list[str]                    # plain-language sentences for the chart caption
    pct_bep: float | None = None
    head_gap_ft: float | None = None    # estimated head minus the curve's; negative = below the curve
    head_gap_pct: float | None = None


def _range(lo: float, hi: float, unit: str) -> str:
    return f"{lo:,.0f}–{hi:,.0f} {unit}"


def check_point(q, h, env: Envelope | None, maker: PumpCurve | None = None, fitted: PumpCurve | None = None,
                settings: CurvesConfig | None = None) -> PointCheck:
    """Compare a running pump's operating point with its maker curve (if any) and its own history."""
    s = settings or CurvesConfig()
    if q is None or h is None or not (np.isfinite(q) and np.isfinite(h)):
        return PointCheck("No reading", "warning", ["It is running, but this hour has no usable flow and pressure "
                                                    "reading, so its point is not shown."])
    flags: list[str] = []
    notes: list[str] = []
    pct_bep = gap = gap_pct = None
    if maker is not None:
        hc = float(maker.head_at(q))
        if np.isfinite(hc) and hc > 0:
            gap, gap_pct = h - hc, 100 * (h - hc) / hc
            if gap_pct <= -s.head_off_curve_pct:
                flags.append(f"Head {abs(gap_pct):.0f}% below curve")
                notes.append(f"Head is {abs(gap):,.0f} ft ({abs(gap_pct):.0f}%) below the maker curve at this flow. "
                             "That can mean wear, or that the static level on file is out of date.")
            elif gap_pct >= s.head_off_curve_pct:
                flags.append(f"Head {gap_pct:.0f}% above curve")
                notes.append(f"Head is {gap:,.0f} ft ({gap_pct:.0f}%) above the maker curve at this flow. Check the "
                             "static level and specific capacity on file, and the curve.")
            else:
                notes.append(f"Head is within {s.head_off_curve_pct:.0f}% of the maker curve ({gap:+,.0f} ft).")
        else:
            flags.append("Off the curve")
            notes.append(f"Flow is outside the maker curve's range "
                         f"({_range(maker.flow_gpm[0], maker.flow_gpm[-1], 'gpm')}).")
        bep = maker.bep_gpm
        if bep:
            pct_bep = 100 * q / bep
            por = f"{s.por_low_pct:.0f}–{s.por_high_pct:.0f}%"
            where = f"{pct_bep:.0f}% of best-efficiency flow ({bep:,.0f} gpm)"
            if pct_bep < s.por_low_pct:
                flags.append(f"{pct_bep:.0f}% of BEP")
                notes.append(f"It runs at {where}, below the preferred range ({por}), which wears it through "
                             "recirculation and vibration.")
            elif pct_bep > s.por_high_pct:
                flags.append(f"{pct_bep:.0f}% of BEP")
                notes.append(f"It runs at {where}, above the preferred range ({por}), which risks cavitation and "
                             "overloading the motor.")
            else:
                notes.append(f"It runs at {where}, inside the preferred range ({por}).")
    elif fitted is not None:
        hc = float(fitted.head_at(q))
        if np.isfinite(hc):
            gap = h - hc
            notes.append(f"It sits {abs(gap):,.0f} ft {'below' if gap < 0 else 'above'} the curve fitted to its "
                         "history.")
    if env is not None:
        q5, q95, h5, h95 = env.usual()
        out = [(name, "low" if v < lo else "high") for name, v, lo, hi in (("flow", q, q5, q95), ("head", h, h5, h95))
               if v < lo or v > hi]
        usual = f"{_range(q5, q95, 'gpm')} and {_range(h5, h95, 'ft')}"
        if out:
            flags.append(", ".join(f"{n} {w}" for n, w in out).capitalize())
            said = " and ".join(f"{n} is {'below' if w == 'low' else 'above'}" for n, w in out)
            notes.append(f"Its {said} its usual range ({usual}, the middle 90% of its running hours).")
        else:
            notes.append(f"It is inside its usual range ({usual}, the middle 90% of its running hours).")
    label = ", ".join(flags[:2]) or "Usual"
    return PointCheck(label, "warning" if flags else "good", notes, pct_bep, gap, gap_pct)


# ----------------------------------------------------------------------------- the curve file
def _csv_rows(path: Path) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """The header and the non-blank rows, each with its line number in the file. Takes UTF-8 (with or
    without the byte-order mark Excel's "CSV UTF-8" writes) and Excel's plain Windows "CSV" (cp1252)."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    reader = csv.reader(io.StringIO(text, newline=""), skipinitialspace=True)
    header: list[str] = []
    rows: list[tuple[int, list[str]]] = []
    for row in reader:
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        if header:
            rows.append((reader.line_num, cells))
        else:
            header = cells
    return header, rows


def load_pump_curves(path: str | Path) -> dict[str, PumpCurve]:
    """Read pump_curves.csv: one row per curve point, with columns pump, flow_gpm, head_ft and optional
    efficiency_pct and source. A missing or header-only file gives {}. Bad rows raise ValueError, naming
    the line, so a typo is reported instead of drawing a wrong curve."""
    path = Path(path)
    if not path.exists():
        return {}
    header, rows = _csv_rows(path)
    missing = [c for c in ("pump", "flow_gpm", "head_ft") if c not in header]
    if header and missing:
        raise ValueError(f"{path.name}: missing column(s) {', '.join(missing)}; expected {', '.join(CURVE_COLUMNS)}")
    if not rows:
        return {}
    errors: list[str] = []
    width = len(header)
    for n, cells in rows:
        if any(cells[width:]):  # an unquoted comma, as in 1,200, shifts every value after it
            errors.append(f"line {n}: {len(cells)} values for {width} columns; write 1,200 as 1200 and put "
                          "double quotes around text that has a comma")
    df = pd.DataFrame([(cells + [""] * width)[:width] for _, cells in rows], columns=header,
                      index=[n for n, _ in rows])
    num = {}
    for col in ("flow_gpm", "head_ft", "efficiency_pct"):
        text = df[col] if col in df else pd.Series("", index=df.index)
        # Excel writes a number shown as 1,200 as "1,200"
        v = pd.to_numeric(text.str.replace(",", "", regex=False).where(text != ""), errors="coerce")
        for i in df.index[(text != "") & v.isna()]:
            errors.append(f"line {i}: {col} {text[i]!r} is not a number")
        num[col] = v
    for i in df.index:
        line = f"line {i}"
        if df.at[i, "pump"] == "":
            errors.append(f"{line}: no pump id")
        if df.at[i, "flow_gpm"] == "" or df.at[i, "head_ft"] == "":
            errors.append(f"{line}: flow_gpm and head_ft are both required")
        if num["flow_gpm"][i] < 0:
            errors.append(f"{line}: flow_gpm must not be negative")
        if num["head_ft"][i] <= 0:
            errors.append(f"{line}: head_ft must be positive")
        e = num["efficiency_pct"][i]
        if pd.notna(e) and not 0 < e <= 100:
            errors.append(f"{line}: efficiency_pct must be between 0 and 100")
    curves: dict[str, PumpCurve] = {}
    for pump, g in df.groupby("pump", sort=False):
        if not pump:
            continue
        q = num["flow_gpm"][g.index]
        if len(g) < MIN_POINTS:
            errors.append(f"{pump}: {len(g)} point(s); give at least {MIN_POINTS} points across the curve")
            continue
        if q.duplicated().any():
            errors.append(f"{pump}: flow {q[q.duplicated()].iloc[0]:g} gpm is listed twice")
            continue
        order = np.argsort(q.to_numpy(float))
        eff = num["efficiency_pct"][g.index].to_numpy(float)[order]
        source = next((v for v in g["source"] if v), "") if "source" in g else ""
        curves[pump] = PumpCurve(pump, "maker", q.to_numpy(float)[order],
                                 num["head_ft"][g.index].to_numpy(float)[order],
                                 eff if np.isfinite(eff).any() else None, source)
    if errors:
        more = f" (and {len(errors) - 5} more)" if len(errors) > 5 else ""
        raise ValueError(f"{path.name}: " + "; ".join(errors[:5]) + more)
    return curves
