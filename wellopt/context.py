"""Load the configuration, run QC and build the pump table in one step."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import SiteConfig, load_config, load_pump_specs
from .data import QCResult, load_history, quality_check
from .pumps import build_pump_table
from .tariff import Tariff


@dataclass
class Context:
    cfg: SiteConfig
    specs: pd.DataFrame
    qc: QCResult
    table: pd.DataFrame
    tariff: Tariff

    def reports_dir(self) -> Path:
        return self.cfg.path(self.cfg.live.reports_dir)

    def models_dir(self) -> Path:
        return self.cfg.path(self.cfg.live.models_dir)

    def store_path(self) -> Path:
        return self.cfg.path(self.cfg.live.store_path)


def tank_columns(raw: pd.DataFrame) -> dict[str, str]:
    """Tank level tags in an export, named TNK_<Section>_MG (usable storage, MG)."""
    return {c.split("_")[1]: c for c in raw.columns if c.startswith("TNK_") and c.endswith("_MG")}


def load_context(config_path: str | Path | None = None, history: str | Path | None = None,
                 raw: pd.DataFrame | None = None, cfg: SiteConfig | None = None) -> Context:
    cfg = cfg or load_config(config_path)
    specs = load_pump_specs(cfg)
    if raw is None:
        raw = load_history(history or cfg.path(cfg.history_path))
    qc = quality_check(raw, specs, tank_columns=tank_columns(raw) or None)
    table = build_pump_table(qc, specs, cfg)
    return Context(cfg, specs, qc, table, Tariff(cfg.tariff))
