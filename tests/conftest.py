"""Shared fixtures.

The synthetic site is written once per session and its context loaded once. Tests
that write files either copy the site (`site`) or point a context at tmp_path with
`synthetic.ctx_in`, so the shared copy is never modified.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests import synthetic as syn
from wellopt.backtest import run as run_backtest
from wellopt.context import load_context
from wellopt.live.runner import make_plans

SMALL_BACKTEST_POLICIES = ["historical", "legacy_hourly", "rule_naive", "milp_naive", "milp_perfect"]


@pytest.fixture(scope="session")
def site_template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("site")
    syn.write_site(root)
    return root


@pytest.fixture(scope="session")
def ctx(site_template):
    return load_context(site_template / "config" / "site.toml")


@pytest.fixture
def site(site_template, tmp_path) -> Path:
    """A private copy of the synthetic site that the test may write into."""
    root = tmp_path / "site"
    shutil.copytree(site_template, root)
    return root


@pytest.fixture(scope="session")
def north_plan(ctx):
    """A 24 h MILP plan for North from PLAN_AT with the seasonal-naive forecast."""
    return make_plans(ctx, syn.PLAN_AT, "naive", "milp", sections=["North"], save_models=False)["North"]


@pytest.fixture(scope="session")
def small_backtest(ctx):
    """Two days of closed-loop simulation for North: (summary table, {(section, policy): SimResult})."""
    return run_backtest(["North"], SMALL_BACKTEST_POLICIES, syn.BT_START, syn.BT_END, qc=ctx.qc, table=ctx.table,
                        cfg=ctx.cfg, tariff=ctx.tariff, replan_every=6)
