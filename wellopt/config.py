"""Site configuration: operating assumptions from config/site.toml plus pump specs."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "site.toml"
SECTIONS = ("North", "South")


@dataclass(frozen=True)
class StorageConfig:
    usable_mg: float
    min_frac: float = 0.30
    floor_frac: float = 0.10
    max_frac: float = 0.95
    initial_frac: float = 0.60
    end_frac: float = 0.55

    @property
    def min_mg(self) -> float:
        return self.usable_mg * self.min_frac

    @property
    def floor_mg(self) -> float:
        return self.usable_mg * self.floor_frac

    @property
    def max_mg(self) -> float:
        return self.usable_mg * self.max_frac

    @property
    def initial_mg(self) -> float:
        return self.usable_mg * self.initial_frac

    @property
    def end_mg(self) -> float:
        return self.usable_mg * self.end_frac


@dataclass(frozen=True)
class OperationsConfig:
    horizon_hours: int = 48
    min_up_hours: int = 4
    min_down_hours: int = 2
    max_starts_per_day: int = 2
    start_cost_usd: float = 10.0
    risk_horizon_hours: int = 6
    replan_every_hours: int = 1
    solver_time_limit_s: float = 20.0
    mip_gap: float = 0.005
    stage_hours: int = 6
    penalty_below_min_usd_per_mg: float = 20_000.0
    penalty_below_floor_usd_per_mg: float = 100_000.0
    penalty_above_max_usd_per_mg: float = 5_000.0
    penalty_end_usd_per_mg: float = 10_000.0


@dataclass(frozen=True)
class TariffConfig:
    off_peak_usd_per_kwh: float = 0.07
    on_peak_usd_per_kwh: float = 0.14
    on_peak_start_hour: int = 14
    on_peak_end_hour: int = 20
    on_peak_weekdays_only: bool = True
    demand_charge_usd_per_kw_month: float = 12.0
    demand_charge_on_peak_only: bool = False


@dataclass(frozen=True)
class ForecastConfig:
    quantiles: tuple[float, ...] = (0.1, 0.5, 0.9)
    origin_every_hours: int = 5
    max_iter: int = 300
    learning_rate: float = 0.05
    calib_days: int = 56
    target: str = "absolute"  # GBM target: "absolute" (gpm) or "ratio" (gpm / trailing week's mean)


@dataclass(frozen=True)
class LiveConfig:
    store_path: str = "var/wellopt.sqlite"
    models_dir: str = "models"
    reports_dir: str = "reports"
    lookback_days: int = 730
    retrain_every_days: int = 28
    stale_after_minutes: int = 120
    plan_retention_days: int = 365
    alert_webhook: str = ""
    weather_enabled: bool = False
    weather_lat: float = 0.0
    weather_lon: float = 0.0
    weather_user_agent: str = "wellopt"
    pi_base_url: str = ""
    pi_tag_map: str = "config/pi_tags.csv"
    pi_verify_tls: bool = True
    pi_ca_bundle: str = ""


@dataclass(frozen=True)
class CurvesConfig:
    """[pump_curves]: maker pump curves and the operating checks on the ops page."""
    path: str = "config/pump_curves.csv"
    por_low_pct: float = 70.0            # preferred operating region, as % of best-efficiency flow
    por_high_pct: float = 120.0
    head_off_curve_pct: float = 10.0     # flag head this far above or below the maker curve
    fit_min_r2: float = 0.45             # show a curve fitted to SCADA only if it falls with flow and fits this well


@dataclass(frozen=True)
class SiteConfig:
    name: str
    timezone: str
    pump_specs_path: Path
    history_path: Path
    power_source: str
    wire_to_water_efficiency: float
    storage: dict[str, StorageConfig]
    ops: OperationsConfig
    tariff: TariffConfig
    forecast: ForecastConfig
    live: LiveConfig
    out_of_service: tuple[str, ...] = ()
    must_run: tuple[str, ...] = ()
    meters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    groups: dict[str, dict[str, float]] = field(default_factory=dict)
    root: Path = REPO_ROOT
    curves: CurvesConfig = field(default_factory=CurvesConfig)

    def path(self, relative: str | Path) -> Path:
        """Resolve a path from the config relative to the repo root."""
        p = Path(relative)
        return p if p.is_absolute() else self.root / p


def _section(d: dict, key: str) -> dict:
    value = d.get(key, {})
    return value if isinstance(value, dict) else {}


def load_config(path: str | Path | None = None) -> SiteConfig:
    """Load config/site.toml (or `path`). Unknown keys are ignored."""
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    root = path.resolve().parent.parent if path.resolve().parent.name == "config" else REPO_ROOT

    site = _section(raw, "site")
    power = _section(raw, "power_model")
    storage = {
        name: StorageConfig(**{k: float(v) for k, v in vals.items()})
        for name, vals in _section(raw, "storage").items()
    }
    for name in SECTIONS:
        storage.setdefault(name, StorageConfig(usable_mg=3.0))

    ops_raw = _section(raw, "operations")
    ops = OperationsConfig(**{k: type(getattr(OperationsConfig, k))(v) for k, v in ops_raw.items()
                              if hasattr(OperationsConfig, k)})
    tariff_raw = _section(raw, "tariff")
    tariff = TariffConfig(**{k: type(getattr(TariffConfig, k))(v) for k, v in tariff_raw.items()
                             if hasattr(TariffConfig, k)})
    fc_raw = dict(_section(raw, "forecast"))
    if "quantiles" in fc_raw:
        fc_raw["quantiles"] = tuple(float(q) for q in fc_raw["quantiles"])
    forecast = ForecastConfig(**{k: v for k, v in fc_raw.items() if hasattr(ForecastConfig, k)})
    if forecast.target not in ("absolute", "ratio"):
        raise ValueError(f"[forecast] target must be 'absolute' or 'ratio', not {forecast.target!r}")
    live = LiveConfig(**{k: v for k, v in _section(raw, "live").items() if hasattr(LiveConfig, k)})
    curves = CurvesConfig(**{k: type(getattr(CurvesConfig, k))(v) for k, v in _section(raw, "pump_curves").items()
                             if hasattr(CurvesConfig, k)})

    pumps = _section(raw, "pumps")
    meters = {m: tuple(p) for m, p in _section(raw, "meters").items()}
    groups = {g: {k: float(v) for k, v in spec.items()} for g, spec in _section(raw, "groups").items()
              if isinstance(spec, dict)}

    return SiteConfig(
        name=site.get("name", "Well system"),
        timezone=site.get("timezone", "UTC"),
        pump_specs_path=Path(site.get("pump_specs", "Well_Summarys.csv")),
        history_path=Path(site.get("history_csv", "Final_Data_hourly.csv")),
        power_source=power.get("source", "recorded"),
        wire_to_water_efficiency=float(power.get("assumed_wire_to_water_efficiency", 0.70)),
        storage=storage,
        ops=ops,
        tariff=tariff,
        forecast=forecast,
        live=live,
        out_of_service=tuple(pumps.get("out_of_service", ())),
        must_run=tuple(pumps.get("must_run", ())),
        meters=meters,
        groups=groups,
        root=root,
        curves=curves,
    )


SPEC_COLUMNS = {
    "Location": "pump",
    "Section": "section",
    "X": "x",
    "Y": "y",
    "Group": "group",
    "Static (ft)": "static_ft",
    "Specfic Capacity (gpm/ft)": "specific_capacity_gpm_ft",
    "Average_Flow(gpm)": "spec_flow_gpm",
    "Average_Press(psi)": "spec_press_psi",
    "Average_Power_Usage(kW-Hr)": "spec_kw",
}


def load_pump_specs(cfg: SiteConfig) -> pd.DataFrame:
    """Pump spec sheet (Well_Summarys.csv) with tidy column names, indexed by pump id."""
    specs = pd.read_csv(cfg.path(cfg.pump_specs_path)).rename(columns=SPEC_COLUMNS)
    specs["group"] = specs["group"].astype(str).where(specs["group"].astype(str) != "0", None)
    specs = specs.set_index("pump")
    specs["meter"] = [meter_of(p, cfg) for p in specs.index]
    return specs


def meter_of(pump: str, cfg: SiteConfig) -> str:
    for meter, pumps in cfg.meters.items():
        if pump in pumps:
            return meter
    return pump
