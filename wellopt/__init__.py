"""wellopt: storage-aware well-pump scheduling.

Pipeline: hourly SCADA data -> quality control -> per-pump models -> probabilistic
demand forecast -> multi-period MILP schedule -> backtest / live advisory loop.
"""

__version__ = "0.2.0"
