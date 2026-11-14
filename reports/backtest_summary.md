# Policy backtest: 2019-06-01 to 2019-07-01

Generated 2026-11-14T00:45:29. Closed loop: every 6 h each storage-aware policy plans 48 h ahead using only data it would have had, the first 6 h of the plan are applied, storage moves with the *actual* demand, and the plan is redone.

## Key findings

* **North**: the best runnable policy, MILP scheduler, GBM forecast, cuts the bill by 8.6% against historical operation (storage-adjusted, recorded kW): energy cost 11.9% lower, demand charges $3,865 vs $3,865. Energy use falls 11.9% with recorded kW and 7.5% with the physics power model. It made 102 pump starts vs 370 historically. Simulated storage was under the 1.05 MG reserve for 21 h, bottoming at 0.50 MG (above the 0.35 MG floor; assumed tank 3.5 MG); the tank was full and spilling for 24 h (1.9 MG).
  A perfect demand forecast would add 10.2 more points of savings.
  The original method (with hindsight) shows 7.6% but needs 864 starts, up to 7 per pump per day.
* **South**: the best runnable policy, MILP scheduler, GBM forecast, cuts the bill by 32.2% against historical operation (storage-adjusted, recorded kW): energy cost 31.9% lower, demand charges $2,000 vs $2,997. Energy use falls 32.0% with recorded kW and 22.8% with the physics power model. It made 63 pump starts vs 296 historically. Simulated storage stayed above the 0.96 MG reserve (lowest 1.05 MG; assumed tank 3.2 MG); the tank was full and spilling for 30 h (3.6 MG).
  A perfect demand forecast would add 2.3 more points of savings.
  The original method (with hindsight) shows 29.6% but needs 947 starts, up to 8 per pump per day.

## North

| policy | bill $ (adj.) | bill saving % | energy $ saving % | kWh saving % | kWh saving % (physics) | demand charge $ | on-peak kWh | starts | max starts/pump/day | min storage MG | h below reserve | spilled MG | unserved MG | fallbacks | mean solve s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Historical operation | 13,876 | 0.0 | 0.0 | 0.0 | 0.0 | 3,865 | 15% | 370 | 3 |  | 0 | 0.0 | 0.00 | 0 |  |
| Original method (hourly cheapest set, hindsight) | 12,822 | 7.6 | 10.5 | 10.4 | 6.3 | 3,865 | 15% | 864 | 7 |  | 0 | 4.4 | 0.00 | 0 |  |
| Storage-aware rule, naive forecast | 13,309 | 4.1 | 5.7 | 6.1 | 1.8 | 3,865 | 16% | 355 | 3 | 0.43 | 32 | 27.0 | 0.00 | 0 | 0.0 |
| Storage-aware rule, GBM forecast | 12,954 | 6.6 | 9.2 | 9.6 | 5.3 | 3,865 | 16% | 291 | 3 | 0.59 | 12 | 5.2 | 0.00 | 0 | 0.0 |
| MILP scheduler, naive forecast | 12,884 | 7.2 | 9.9 | 10.0 | 5.6 | 3,865 | 16% | 120 | 2 | 0.21 | 40 | 14.2 | 0.00 | 0 | 3.6 |
| MILP scheduler, GBM forecast | 12,690 | 8.6 | 11.9 | 11.9 | 7.5 | 3,865 | 16% | 102 | 2 | 0.50 | 21 | 1.9 | 0.00 | 0 | 5.2 |
| MILP scheduler, perfect forecast (bound) | 11,281 | 18.7 | 12.1 | 12.3 | 7.7 | 2,479 | 16% | 44 | 2 | 1.05 | 0 | 0.0 | 0.00 | 0 | 3.2 |

Planning-flow check: the historical on/off states at the planning flows deliver 97.7% of the production actually recorded in this window (close enough for a fair comparison).

## South

| policy | bill $ (adj.) | bill saving % | energy $ saving % | kWh saving % | kWh saving % (physics) | demand charge $ | on-peak kWh | starts | max starts/pump/day | min storage MG | h below reserve | spilled MG | unserved MG | fallbacks | mean solve s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Historical operation | 12,778 | 0.0 | 0.0 | 0.0 | 0.0 | 2,997 | 16% | 296 | 3 |  | 0 | 0.0 | 0.00 | 0 |  |
| Original method (hourly cheapest set, hindsight) | 8,991 | 29.6 | 31.0 | 30.8 | 21.8 | 2,239 | 16% | 947 | 8 |  | 0 | 3.2 | 0.00 | 0 |  |
| Storage-aware rule, naive forecast | 10,233 | 19.9 | 28.5 | 28.5 | 19.9 | 3,239 | 16% | 336 | 3 | 1.08 | 0 | 16.5 | 0.00 | 0 | 0.0 |
| Storage-aware rule, GBM forecast | 9,058 | 29.1 | 30.3 | 30.5 | 21.8 | 2,239 | 16% | 234 | 3 | 1.22 | 0 | 6.8 | 0.00 | 0 | 0.0 |
| MILP scheduler, naive forecast | 9,995 | 21.8 | 30.9 | 31.1 | 21.8 | 3,239 | 16% | 88 | 2 | 0.78 | 7 | 8.0 | 0.00 | 0 | 4.3 |
| MILP scheduler, GBM forecast | 8,661 | 32.2 | 31.9 | 32.0 | 22.8 | 2,000 | 16% | 63 | 2 | 1.05 | 0 | 3.6 | 0.00 | 0 | 2.9 |
| MILP scheduler, perfect forecast (bound) | 8,368 | 34.5 | 32.4 | 32.6 | 23.5 | 1,758 | 16% | 26 | 1 | 0.96 | 0 | 0.0 | 0.00 | 0 | 2.7 |

Planning-flow check: the historical on/off states at the planning flows deliver 99.1% of the production actually recorded in this window (close enough for a fair comparison).

## How to read this

* **Dollars are relative.** Costs use the recorded kW, which fails the physics check (see `data_audit.md`). Percent savings mean more than dollar amounts. The physics column re-costs the same on/off schedules with the physics power model; when both columns agree, the ranking does not depend on the kW problem.
* **Storage-adjusted.** A policy that ends the window with less water in storage is charged for refilling it at its own average cost per MG, and credited if it ends with more, so no policy wins by draining the tank.
* **The simulated tank is physical.** It cannot rise above the usable volume or fall below empty. Water pumped into a full tank is spilled and its energy still counts; demand an empty tank cannot meet is unserved, charged at the policy's average cost per MG, and rules the policy out of the key findings.
* **Historical and the original method have flat storage.** The export has no tank levels, so demand is taken to be historical production. Only the storage-aware policies use storage (North 3.5 MG, South 3.2 MG, ASSUMED). Their savings come from moving pumping in time and grow with the tank, so rerun this with the real usable volumes.
* **The original method is a hindsight bound, not a policy.** It picks the cheapest pump set for each hour's actual production, ignores storage, start limits and minimum run times, and spills any surplus. Its start count shows why it could not be run as is.
* **Perfect-forecast rows are bounds.** The gap between the GBM and perfect-forecast rows is the most a better demand forecast could add.
* **Demand charges dominate short windows.** Each month's charge counts only the amount by which a policy raises that month's peak above the peak already set before the window started, so a window that starts on the 1st carries the whole month's charge. The default window is one whole billing month for that reason; the energy and kWh columns are free of this effect.
* Hours with an unknown pump state count as off in the historical row, which slightly understates historical cost (conservative).

## Assumptions to confirm

* North usable storage 3.50 MG (min 30%, floor 10%, max 95%). No tank telemetry in the export, so demand = production and storage is simulated.
* South usable storage 3.20 MG (min 30%, floor 10%, max 95%). No tank telemetry in the export, so demand = production and storage is simulated.
* Tariff: 0.070 $/kWh off-peak, 0.140 $/kWh on-peak 14:00-20:00 weekdays, demand charge 12.00 $/kW-month, one meter per pump.
* Pump rules: min up 4 h, min down 2 h, max 2 starts per pump per day, $10 per start.
* Power model for planning: recorded kW (physics model assumes 70% wire-to-water efficiency).
* Timestamps are local wall clock (America/Chicago); the spring daylight-saving hour is missing.
* Out of service: PMP_5, PMP_20, PMP_26. Must run: none.
