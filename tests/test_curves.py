"""Pump curves: the head estimate, the curve file as Excel writes it, the best-efficiency point, curves
fitted to a pump's history, its usual range, and the check of where it runs now."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wellopt.config import CurvesConfig
from wellopt.curves import (MIN_ENVELOPE_HOURS, MIN_FIT_HOURS, PumpCurve, check_point, envelope, fit_field_curve,
                            load_pump_curves)
from wellopt.pumps import total_dynamic_head

HEADER = "pump,flow_gpm,head_ft,efficiency_pct,source\n"
POINTS = [(400, 318, 38), (800, 300, 60), (1200, 272, 74), (1500, 248, 78), (1800, 218, 75), (2100, 180, 66)]
GOOD = HEADER + "".join(f"PMP_13,{q},{h},{e},test curve\n" for q, h, e in POINTS)
BEP = 1521.4  # the parabola through (1200, 74), (1500, 78) and (1800, 75) peaks here


def write(tmp_path, text: str, encoding: str = "utf-8", name: str = "pump_curves.csv"):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


@pytest.fixture
def maker() -> PumpCurve:
    q, h, e = (np.array(v, dtype=float) for v in zip(*POINTS))
    return PumpCurve("PMP_13", "maker", q, h, e, "test curve")


def history(q: np.ndarray, h: np.ndarray) -> tuple[pd.Series, pd.Series]:
    idx = pd.date_range("2019-01-01", periods=len(q), freq="h")
    return pd.Series(q, index=idx), pd.Series(h, index=idx)


# ----------------------------------------------------------------------------- head
def test_total_dynamic_head():
    assert total_dynamic_head(1500.0, 60.0, 100.0, 20.0) == pytest.approx(100 + 75 + 60 * 2.31)
    # no specific capacity on file: no drawdown term
    assert total_dynamic_head(1500.0, 60.0, 100.0, float("nan")) == pytest.approx(100 + 60 * 2.31)
    assert total_dynamic_head(1500.0, 60.0, 100.0, 0.0) == pytest.approx(100 + 60 * 2.31)
    h = total_dynamic_head(pd.Series([1000.0, np.nan, 1200.0]), pd.Series([50.0, 50.0, np.nan]), 30.0, 10.0)
    assert h.iloc[0] == pytest.approx(30 + 100 + 50 * 2.31)
    assert h.iloc[1:].isna().all()


# ----------------------------------------------------------------------------- the curve file
def test_load_sorts_points_and_finds_the_bep(tmp_path):
    rows = GOOD.splitlines(keepends=True)
    path = write(tmp_path, rows[0] + "".join(reversed(rows[1:])))  # points in any order
    curves = load_pump_curves(path)
    assert list(curves) == ["PMP_13"]
    c = curves["PMP_13"]
    assert c.kind == "maker" and c.source == "test curve"
    assert c.flow_gpm.tolist() == [q for q, _, _ in POINTS]
    assert c.head_ft.tolist() == [h for _, h, _ in POINTS]
    assert c.bep_gpm == pytest.approx(BEP, abs=0.1)


def test_missing_empty_and_header_only_files_have_no_curves(tmp_path):
    assert load_pump_curves(tmp_path / "missing.csv") == {}
    assert load_pump_curves(write(tmp_path, "", name="empty.csv")) == {}
    assert load_pump_curves(write(tmp_path, HEADER, name="header.csv")) == {}
    assert load_pump_curves(write(tmp_path, HEADER + "\n,,,,\n", name="blank_rows.csv")) == {}


def test_the_source_column_is_optional(tmp_path):
    text = "pump,flow_gpm,head_ft\n" + "".join(f"PMP_13,{q},{h}\n" for q, h, _ in POINTS)
    c = load_pump_curves(write(tmp_path, text))["PMP_13"]
    assert c.source == "" and c.efficiency_pct is None and c.bep_gpm is None


@pytest.mark.parametrize("encoding, text", [
    ("utf-8", "﻿" + GOOD.replace("\n", "\r\n")),        # Excel "CSV UTF-8": byte-order mark, CRLF
    ("cp1252", GOOD.replace("test curve", "Café test")),    # Excel's plain "CSV" on Windows
])
def test_reads_what_excel_writes(tmp_path, encoding, text):
    c = load_pump_curves(write(tmp_path, text, encoding))["PMP_13"]
    assert c.flow_gpm.tolist() == [q for q, _, _ in POINTS]
    assert c.source in ("test curve", "Café test")


def test_quoted_commas_and_blank_lines(tmp_path):
    text = (HEADER + "\n" + 'PMP_13,"1,200",272,74,"Maker test, 2019"\n' + "\n"
            + "".join(f"PMP_13,{q},{h},{e},\n" for q, h, e in POINTS if q != 1200))
    c = load_pump_curves(write(tmp_path, text))["PMP_13"]
    assert 1200 in c.flow_gpm.tolist() and len(c.flow_gpm) == len(POINTS)
    assert c.source == "Maker test, 2019"
    # line numbers count the blank lines, so they match what Excel or Notepad shows
    bad = HEADER + "\n" + "".join(f"PMP_13,{q},{h},{e},\n" for q, h, e in POINTS[:2]) + "PMP_13,1200,-1,74,\n"
    with pytest.raises(ValueError, match="line 5: head_ft must be positive"):
        load_pump_curves(write(tmp_path, bad, name="bad.csv"))


GOOD_ROWS = "".join(f"PMP_13,{q},{h},{e},\n" for q, h, e in POINTS)


@pytest.mark.parametrize("rows, message", [
    ("PMP_13,1,250,268,74,test\n", "line 2: 6 values for 5 columns; write 1,200 as 1200"),
    (GOOD_ROWS + "PMP_13,2,400,150,50,test\n", f"line {len(POINTS) + 2}: 6 values for 5 columns"),
    (",1250,268,74,\n", "line 2: no pump id"),
    ("PMP_13,,268,74,\n", "line 2: flow_gpm and head_ft are both required"),
    ("PMP_13,abc,268,74,\n", "line 2: flow_gpm 'abc' is not a number"),
    ("PMP_13,-5,268,74,\n", "line 2: flow_gpm must not be negative"),
    ("PMP_13,1250,0,74,\n", "line 2: head_ft must be positive"),
    ("PMP_13,1250,268,140,\n", "line 2: efficiency_pct must be between 0 and 100"),
    ("PMP_13,1200,270,74,\n", "PMP_13: flow 1200 gpm is listed twice"),
    ("PMP_99,1000,200,70,\nPMP_99,1500,180,75,\n", "PMP_99: 2 point(s); give at least 3 points"),
])
def test_bad_rows_are_reported_by_line(tmp_path, rows, message):
    text = HEADER + (rows if rows.startswith(GOOD_ROWS) else rows + GOOD_ROWS)
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        load_pump_curves(write(tmp_path, text))


def test_an_unquoted_comma_that_lines_up_is_caught_by_the_checks(tmp_path):
    # "1,200" unquoted with no source column: flow 1, head 200, efficiency 272
    text = "pump,flow_gpm,head_ft,efficiency_pct\nPMP_13,1,200,272\n"
    with pytest.raises(ValueError, match="line 2: efficiency_pct must be between 0 and 100"):
        load_pump_curves(write(tmp_path, text))


def test_missing_columns_and_many_errors(tmp_path):
    with pytest.raises(ValueError, match="missing column.s. flow_gpm; expected pump, flow_gpm"):
        load_pump_curves(write(tmp_path, "pump,flow,head_ft\nPMP_13,1200,272\n"))
    text = HEADER + "".join(f"PMP_13,{q},-1,74,\n" for q in (100, 200, 300, 400, 500, 600, 700))
    with pytest.raises(ValueError, match=r"pump_curves.csv: line 2: head_ft must be positive; .*\(and 2 more\)$"):
        load_pump_curves(write(tmp_path, text))


# ----------------------------------------------------------------------------- the curve itself
def test_head_on_the_curve(maker):
    assert maker.head_at(400) == pytest.approx(318)
    assert maker.head_at(1000) == pytest.approx(286)  # halfway between 800 and 1200
    assert np.isnan(maker.head_at(300)) and np.isnan(maker.head_at(2200))
    assert np.isnan(maker.head_at([300, 1000]))[0] and maker.head_at([300, 1000])[1] == pytest.approx(286)


def test_bep_needs_efficiencies(maker):
    assert maker.bep_gpm == pytest.approx(BEP, abs=0.1)
    assert PumpCurve("P", "maker", maker.flow_gpm, maker.head_ft).bep_gpm is None
    assert PumpCurve("P", "maker", maker.flow_gpm, maker.head_ft, np.full(len(POINTS), np.nan)).bep_gpm is None
    rising = np.array([40, 50, 60, 70, 75, 80], dtype=float)  # best at the last point: no parabola to fit
    assert PumpCurve("P", "maker", maker.flow_gpm, maker.head_ft, rising).bep_gpm == 2100


# ----------------------------------------------------------------------------- history
def test_a_falling_history_gives_a_credible_fit():
    rng = np.random.default_rng(1)
    q = rng.uniform(800, 1600, 300)
    h = 400 - 4e-5 * q ** 2 + rng.normal(0, 2, q.size)
    fit = fit_field_curve(q, h)
    assert fit.hours == 300 and fit.k == pytest.approx(-4e-5, rel=0.05) and fit.h0 == pytest.approx(400, abs=3)
    assert fit.credible(0.45) and not fit.rises
    curve = fit.curve("PMP_1")
    assert curve.kind == "fitted" and curve.r2 == fit.r2 and "300 running hours" in curve.source
    assert curve.flow_gpm[0] == pytest.approx(fit.flow_lo) and curve.flow_gpm[-1] == pytest.approx(fit.flow_hi)
    assert curve.head_at(1200) == pytest.approx(400 - 4e-5 * 1200 ** 2, abs=3)


def test_a_rising_history_is_flagged_and_not_drawn():
    rng = np.random.default_rng(2)
    q = rng.uniform(800, 1600, 300)
    fit = fit_field_curve(q, 200 + 4e-5 * q ** 2 + rng.normal(0, 2, q.size))
    assert fit.rises and not fit.credible(0.45)


def test_too_few_hours_to_fit():
    q = np.r_[np.linspace(800, 1600, MIN_FIT_HOURS - 1), np.full(20, np.nan)]
    assert fit_field_curve(q, 400 - 4e-5 * q ** 2) is None
    flat = fit_field_curve(np.full(60, 1000.0), np.full(60, 300.0))  # one point, no scatter
    assert flat is not None and not flat.credible(0.45) and not flat.rises


def test_envelope_quantiles_and_sample():
    rng = np.random.default_rng(3)
    q, h = history(rng.normal(1000, 50, 1000), rng.normal(300, 5, 1000))
    env = envelope(q, h)
    assert env.hours == 1000 and env.since == q.index[0]
    assert env.flow[0.5] == pytest.approx(q.median()) and env.head[0.95] == pytest.approx(h.quantile(0.95))
    assert env.usual() == (env.flow[0.05], env.flow[0.95], env.head[0.05], env.head[0.95])
    assert len(env.sample_flow) == len(env.sample_head) == 300
    assert ((env.sample_flow >= env.flow[0.01]) & (env.sample_flow <= env.flow[0.99])).all()
    assert ((env.sample_head >= env.head[0.01]) & (env.sample_head <= env.head[0.99])).all()
    again = envelope(q, h)
    assert (again.sample_flow == env.sample_flow).all()  # the same hours on every refresh


def test_envelope_needs_a_day_of_running_hours():
    q, h = history(np.full(MIN_ENVELOPE_HOURS + 5, 1000.0), np.full(MIN_ENVELOPE_HOURS + 5, 300.0))
    h.iloc[:6] = np.nan
    assert envelope(q, h) is None
    assert envelope(q.iloc[6:], h.iloc[6:]) is None
    full = envelope(*history(np.full(MIN_ENVELOPE_HOURS, 1000.0), np.full(MIN_ENVELOPE_HOURS, 300.0)))
    assert full is not None and len(full.sample_flow) == MIN_ENVELOPE_HOURS


# ----------------------------------------------------------------------------- the check
@pytest.fixture
def env():
    rng = np.random.default_rng(4)
    return envelope(*history(rng.normal(1400, 40, 500), rng.normal(258, 4, 500)))


def test_no_reading(env, maker):
    for q, h in ((None, 250.0), (1400.0, None), (np.nan, 250.0)):
        c = check_point(q, h, env, maker)
        assert (c.label, c.level) == ("No reading", "warning") and "no usable flow and pressure" in c.notes[0]


def test_on_the_curve_inside_the_preferred_range_and_usual_range(env, maker):
    c = check_point(1400.0, 258.0, env, maker)
    assert (c.label, c.level) == ("Usual", "good")
    assert c.pct_bep == pytest.approx(100 * 1400 / BEP, abs=0.1)
    assert c.head_gap_ft == pytest.approx(258 - maker.head_at(1400))
    assert "within 10% of the maker curve" in c.notes[0]
    assert "inside the preferred range (70–120%)" in c.notes[1]
    assert "inside its usual range" in c.notes[2]


@pytest.mark.parametrize("q, h, label, words", [
    (1400.0, 225.0, "Head 12% below curve", "below the maker curve"),
    (1400.0, 290.0, "Head 13% above curve", "above the maker curve"),
    (2400.0, 150.0, "Off the curve", "outside the maker curve's range (400–2,100 gpm)"),
    (900.0, 293.0, "59% of BEP", "recirculation"),
    (2000.0, 192.0, "131% of BEP", "cavitation"),
])
def test_flags_against_the_maker_curve(maker, q, h, label, words):
    c = check_point(q, h, None, maker)
    assert c.label.startswith(label) and c.level == "warning"
    assert any(words in n for n in c.notes)


def test_flags_against_its_usual_range(env):
    c = check_point(1600.0, 240.0, env)
    assert c.label == "Flow high, head low" and c.level == "warning" and c.pct_bep is None
    assert "Its flow is above and head is below its usual range" in c.notes[0]
    assert check_point(1300.0, 258.0, env).label == "Flow low"
    assert check_point(1400.0, 258.0, None).label == "Usual"  # nothing to compare with


def test_the_label_keeps_the_first_two_flags(env, maker):
    c = check_point(900.0, 330.0, env, maker)  # above the curve, far below BEP, outside its usual range
    assert c.label == "Head 13% above curve, 59% of BEP"
    assert len(c.notes) == 3


def test_a_fitted_curve_is_used_without_a_maker_curve(maker):
    fitted = PumpCurve("PMP_13", "fitted", maker.flow_gpm, maker.head_ft, None, "fitted", 0.8)
    c = check_point(1400.0, 250.0, None, None, fitted)
    assert c.label == "Usual" and "sits 6 ft below the curve fitted to its history" in c.notes[0]
    assert check_point(1400.0, 250.0, None, maker, fitted).notes[0].startswith("Head is within")  # maker wins


def test_settings_move_the_limits(maker):
    strict = CurvesConfig(por_low_pct=95, por_high_pct=105, head_off_curve_pct=2)
    c = check_point(1400.0, 250.0, None, maker, settings=strict)
    assert c.label == "Head 2% below curve, 92% of BEP"
    assert any("preferred range (95–105%)" in n for n in c.notes)
