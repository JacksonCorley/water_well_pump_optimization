# Methodology

How the scheduler turns an hourly SCADA export into pump recommendations, and how it was checked.
The numbers here come from the reports in `reports/`; the command that regenerates each one is named
in its section.

## 1. Data

`Final_Data_hourly.csv` holds hourly flow (gpm), discharge pressure (psi) and power (kW) for 31 wells in
two sections, North and South, from 7 Jul 2016 to 7 Jul 2019. `Well_Summarys.csv` gives each well's
group, map position, static water level and specific capacity. PMP_5, PMP_20 and PMP_26 are marked out
of service in `config/site.toml`, which leaves 28 wells to schedule.

**Time stamps** are local wall-clock hours with daylight saving: 02:00 is missing every March. The code
keeps naive local hours throughout, and the PI adapter converts the historian's UTC to the same
convention.

**Quality control** (`wellopt/data.py`) rejects flow or kW readings below zero or far above the pump's
spec (2× its flow, 4× its kW) and negative pressure while a pump runs, and counts hours with flow but no
power. A section's production is the sum of its pumps' flow. It is left unknown when any pump reading is
unknown or rejected, when every pump reads zero (a telemetry outage), or when it exceeds 1.2× the
section's capacity. There is deliberately no statistical spike filter: multi-hour steps in production
are operating decisions, not errors. For model inputs only, gaps are filled (up to 3 hours by
interpolation, longer ones from the same hour a week earlier); filled hours are never used to score a
model. Of 26,303 hourly slots, 93 are missing from the export, and 128 (North) and 122 (South) hours
are filled.

**Demand is taken to be production.** The export has no tank levels, so demand can't be separated from
storage changes. Over a day the difference mostly cancels; hour by hour it doesn't, so the forecast is
really a forecast of what operators pumped. When tank levels are available (`TNK_<Section>_MG` columns),
`reconstruct_demand` computes demand as production minus the change in storage, and everything
downstream uses that instead.

## 2. The energy data fails a physics check

Lifting water takes at least Q × H / 3960 horsepower (Q in gpm, H in feet; 1 psi = 2.31 ft). Using the
discharge pressure alone, with no well lift and a perfect pump and motor, gives a floor no real pump can
get under. `python -m wellopt audit` (`reports/data_audit.md`) finds:

* **28 of 28 pumps record less power than that floor**; the median implied efficiency is 263%. Real
  wire-to-water efficiency is 50–75%.
* The recorded kW still tracks flow × pressure closely (median R² 0.97), and a physics estimate (well
  lift included, 70% efficiency) is a steady 7.0× the recorded value (10th–90th percentile 6.3–8.1×).
  That points to a scaling or aggregation error in the kW tag, not to noise.

Consequences:

* Absolute kWh and dollar figures, including the 2022 project's savings claims, can't be trusted until
  the kW tag is reconciled with utility bills.
* Rankings hold up better. The rank correlation of kWh per thousand gallons under recorded and physics
  power is 0.93 (North) and 0.73 (South). Savings measured **relative to historical operation** are
  therefore more meaningful than dollars, and the backtest reports kWh savings under both power models.
* Setting `[power_model] source = "physics"` plans with the physics model instead.

## 3. Pump models

Each pump has a constant planning flow and power: the means over its valid running hours
(`wellopt/pumps.py`). The physics alternative is kW = 0.746 × Q × (discharge head + lift) / (3960 × η),
with lift = static level + Q / specific capacity and η from the config.

The merit order (kWh per thousand gallons) drives both the rule-based plan and much of the MILP's
choice. Historically PMP_4 ran 63% of hours while using about 2.7× the energy per gallon of the
next-worst South pump; if it runs for a reason the data doesn't show (pressure zone, water quality,
permits), list it under `[pumps] must_run`.

Known limits: pumps on a shared header interact (each extra pump adds less flow), and well efficiency
drifts over time. `[groups]` can cap a shared header's or plant's flow. A better model would fit flow
and power against the number of pumps running in each group, and refit them regularly.

## 4. Electricity tariff

`wellopt/tariff.py` prices energy by time of use (on-peak 14:00–20:00 on weekdays by default) and bills
a monthly demand charge on each meter's highest hourly kW, over all hours or on-peak hours only. Each
pump is its own meter unless `[meters]` groups them. Plans carry the month-to-date peak, so a plan is
only charged for raising it. **The default rates are placeholders**; replace them with the utility's
tariff.

## 5. Demand forecast

Both forecasters (`wellopt/forecast.py`) predict hours 1–48 after an origin using data up to the origin
only.

* **Seasonal naive** (the baseline): the average of the same hour on the most recent full day and the
  same hour a week earlier. Its quantiles come from its own errors over the last 180 days.
* **Quantile gradient boosting**: scikit-learn `HistGradientBoostingRegressor` with quantile loss, one
  model per quantile (P10, P50, P90), with the lead time as a feature so one model covers every lead
  time. Features: lead time, hour, day of week, weekend, US federal holiday, day of year (sine and
  cosine), the same hour on the latest full day and one and two weeks earlier, the four-week mean for
  that hour, the last value, and the 24-hour and 168-hour mean levels. Training origins are every
  5 hours; 5 shares no factor with 24, so every hour of the day serves as an origin. It predicts flow in
  gpm. `[forecast] target = "ratio"` predicts flow as a multiple of the trailing week's mean instead,
  with the lag features scaled the same way; the results below explain why that isn't the default.
* **Calibration**: raw quantile models give bands that are too narrow. The last 56 days are held out,
  and the P10 and P90 are shifted, per lead time, so they are hit at their nominal rates on that window
  (split conformal). The median is left as fitted; shifting it toward the last few weeks' bias chased
  the season and doubled the South bias in testing.
* **Volume band for the scheduler**: storage depends on cumulative demand, and adding up hourly P90s
  would badly overstate the volume at risk. Instead, for each lead time h, the band is the outer
  quantiles of the median forecast's cumulative error over hours 1..h on the held-out window. That gives
  a high-demand and a low-demand path.

`python -m wellopt forecast-eval` backtests both models with a rolling origin every 24 hours over the
last 182 days. Models are refit every 28 days on data before each origin, and only real (not filled)
hours are scored. It reports MAE, MAPE, bias, pinball loss and P10–P90 coverage by lead-time bucket, plus
the error in day-1 and day-2 volume (`reports/forecast_eval.md`).

**Results**, 179 daily origins from 6 January to 7 July 2019:

| | North naive | North GBM | South naive | South GBM |
|---|---|---|---|---|
| Hourly MAE, gpm | 1,816 | 1,675 | 2,318 | 2,121 |
| Pinball loss (P10, P50, P90) | 587 | 530 | 738 | 654 |
| Bias, gpm | −160 | −479 | −264 | 0 |
| Hourly P10–P90 coverage (nominal 80%) | 77% | 77% | 80% | 78% |
| Day-1 volume error, as mean gpm | 992 | 1,015 | 978 | 1,049 |
| Day-1 volume band coverage (nominal 80%) | 69% | 67% | 76% | 75% |

* **Hour by hour the GBM wins**: 8% lower MAE and 10–11% lower pinball loss than the baseline in both
  sections, with hourly bands close to their nominal coverage.
* **On day-ahead volume it doesn't**, and volume is what storage depends on. Its day-1 volume error is 2%
  (North) and 7% (South) larger than the baseline's. By month, it is better in North from January to
  April and in June, and much worse in May (2,023 vs 1,245 gpm) and the first week of July: the months
  when demand climbed to levels the training data hadn't reached. North averaged 12,200 gpm in April and
  15,200 in May. Trees can't predict above the largest values they were trained on, so the GBM
  under-forecasts the early-summer ramp; North's bias in May was −1,600 gpm (−11%).
* **The volume bands are too narrow**: they held day-1 volume on 67–76% of days, against 80% nominal. A
  56-day calibration window can't anticipate a seasonal ramp. The scheduler's reserve and frequent
  re-planning absorb part of the error (section 7).
* **The ratio target** lets the model see a ramp as a level it has seen before. It lowers average error
  everywhere: hourly MAE 1,629 gpm (North) and 2,089 (South), North bias −383 gpm, and day-1 volume
  error 971 and 1,022 gpm, which beats the baseline in North. But a ratio is capped by the largest one
  seen in training, too. On Saturday 1 June 2019, North demand jumped from about 9,400 gpm at 03:00 to
  24,000 gpm by 07:00 and stayed above 20,000 until 11:00. Both models under-forecast it; in the
  closed-loop backtest the ratio-driven plan let the simulated North tank fall to 0.001 MG, where the
  absolute model's plans bottomed out at 0.50 MG over the month. The absolute model stays the default
  until a model handles such surges.

Weather isn't used yet: the repo has no historical weather for the same hours. It is the obvious
missing input, since a May ramp and a Saturday-morning surge look like irrigation, which follows
temperature and rain. The live runner can show the National Weather Service forecast next to the plan,
and `QuantileGBM` already accepts weather columns (`exog`) once matching history is available.

## 6. Scheduler

`wellopt/milp.py` builds and solves the problem, and `wellopt/scheduler.py` turns the plant state and
the forecast into a plan with plain-language notes.

**Decision**: u[p, t] = 1 if pump p runs in hour t, for the next 48 hours.

**Objective**: time-of-use energy cost, plus $10 per pump start (a wear proxy), plus the demand charge
on any increase over the month's billed peak, plus penalties for storage outside its limits.

**Storage**: S[t+1] = S[t] + 60 × 10⁻⁶ × (Σₚ Qₚ u[p, t] − D[t]) in million gallons, with D the median
forecast. The high-demand path differs from the median path by a known amount each hour, so one
storage variable per hour covers both:

* the high-demand path must stay above the reserve (30% of the tank) and the floor (10%) over the first
  6 hours (the risk horizon), and, after that, at its 6-hour offset. Plans are redone every hour, so a
  6-hour risk window applies all along instead of a 48-hour cumulative band;
* the median path must stay below 95%. Running high only wastes energy, and the next plan corrects it;
* storage at the end of the horizon must reach 55%, so plans don't borrow from tomorrow.

Every storage limit is soft, with penalties of $20,000/MG below the reserve, $100,000/MG below the
floor, $5,000/MG above the maximum and $10,000/MG short of the end target. The model is always
feasible, and a shortfall shows up as a penalty and an alert instead of a solver failure.

**Operating rules**: minimum up time 4 hours and down time 2 hours (counting the state each pump is in
at the start), at most 2 starts per pump in each 24-hour block of the plan, must-run and out-of-service
pumps, and optional group flow limits. Starts made before the plan aren't counted, so the backtest also
reports the most starts any pump made in one calendar day.

**Solving**: HiGHS through `scipy.optimize.milp`, using relax-and-fix. Hours 0–5 are integer and later
hours are relaxed to [0, 1]; those six hours are then fixed and the next six solved, and so on. Keeping
every pump in its last fixed state is always feasible, so every stage has a solution. The 20-second
limit is split across the stages, with a 0.5% gap. In tests on three planning hours, six-hour stages
matched or beat a 30-second whole-horizon solve in 3–6 seconds. The whole-horizon solve sometimes
stalled with large penalties: South on 31 Mar 2019 still carried $5,073 of storage penalties after
30 seconds, against $16 with stages.

Plan status is `optimal` (one stage solved to the gap), `staged` (every stage solved to the gap) or
`time_limit` (a stage hit its limit and its best solution was used). If the MILP fails, the runner falls
back to the rule below and raises an alert.

**Rule plan** (the fallback, and a baseline): each hour, aim to produce median demand plus enough to
close the gap to a target level over 6 hours. Keep running pumps, add the cheapest pumps (kWh per
gallon) when short, and drop the most expensive ones while the target is still met. Minimum up and down
times, must-run and out-of-service pumps are respected; the starts limit is not.

## 7. Closed-loop backtest

`python -m wellopt backtest` (`wellopt/backtest.py`, `reports/backtest_summary.md`) replays a past
month as if the scheduler had been running.

* **Window**: by default the last whole billing month that still has 48 hours of data after it (June
  2019), so demand charges are billed over a real month and the perfect-forecast bound can look ahead.
* **Model predictive control**: each storage-aware policy plans 48 hours, runs the first 6 hours of the
  plan against actual demand, then plans again from the state it reached. The live runner replans
  every hour; 6 hours keeps a month-long backtest to about 15 minutes with `--jobs 4`. Forecasts see
  only data up to the hour they are made.
* **Policies**:

  | Policy | What it is |
  |---|---|
  | `historical` | What operators did (on/off states from SCADA). |
  | `legacy_hourly` | The 2022 method: each hour, the cheapest pump set that covers that hour's actual production. It has perfect hindsight, ignores storage and switches pumps freely, so it is a bound, not a policy. |
  | `rule_naive`, `rule_gbm` | The rule plan driven by each forecast. |
  | `milp_naive`, `milp_gbm` | The MILP driven by each forecast. |
  | `milp_perfect`, `rule_perfect` | The same with actual demand as the forecast: an upper bound on what better forecasts could add. |

* **Tank**: starts at 60% full. It is physical: it can't go above the usable volume or below empty.
  Water pumped into a full tank is spilled (its energy still counts), and demand an empty tank can't
  meet is unserved.
* **Costs**: every policy is costed with the same pump power model and tariff. The storage-adjusted bill
  adds the water a policy owes (unserved demand minus the change in storage over the month), valued at
  its own average cost per MG, so no policy can win by draining the tank or by not delivering water. A
  policy that leaves any demand unserved is left out of the key findings.
* **Historical and legacy have flat storage** by construction: demand is taken to be production, so
  they meet it exactly.
* **Reported**: bill and energy cost, kWh under both power models, demand charge, on-peak share of kWh,
  pump starts, the most starts by one pump in one day, minimum storage, hours below the reserve, spilled
  and unserved water, MILP fallbacks and time limits, and solve times.

**Results, June 2019.** Bill savings are against historical operation and storage-adjusted; kWh savings
are shown with the recorded kW and with the physics power model.

| Policy | North bill | North kWh | South bill | South kWh | Starts, North / South |
|---|---|---|---|---|---|
| Historical operation | | | | | 370 / 296 |
| Original method (hindsight) | 7.6% | 10.4% / 6.3% | 29.6% | 30.8% / 21.8% | 864 / 947 |
| Rule, naive forecast | 4.1% | 6.1% / 1.8% | 19.9% | 28.5% / 19.9% | 355 / 336 |
| Rule, GBM forecast | 6.6% | 9.6% / 5.3% | 29.1% | 30.5% / 21.8% | 291 / 234 |
| MILP, naive forecast | 7.2% | 10.0% / 5.6% | 21.8% | 31.1% / 21.8% | 120 / 88 |
| MILP, GBM forecast | 8.6% | 11.9% / 7.5% | 32.2% | 32.0% / 22.8% | 102 / 63 |
| MILP, perfect forecast (bound) | 18.7% | 12.3% / 7.7% | 34.5% | 32.6% / 23.5% | 44 / 26 |

Simulated storage, with reserves of 1.05 MG (North) and 0.96 MG (South) and floors of 0.35 and 0.32 MG:

| Policy | North lowest | North hours under reserve | South lowest | South hours under reserve |
|---|---|---|---|---|
| Rule, naive forecast | 0.43 MG | 32 | 1.08 MG | 0 |
| Rule, GBM forecast | 0.59 MG | 12 | 1.22 MG | 0 |
| MILP, naive forecast | 0.21 MG | 40, 5 of them under the floor | 0.78 MG | 7 |
| MILP, GBM forecast | 0.50 MG | 21 | 1.05 MG | 0 |
| MILP, perfect forecast | 1.05 MG | 0 | 0.96 MG | 0 |

* **The MILP with the GBM forecast is the best runnable policy in both sections**, with about a quarter
  of the starts operators made. Re-costed with the physics power model its kWh savings shrink, but the
  ranking of policies barely changes, so the result doesn't come from the kW scaling problem.
* **Demand charges explain North's gap to the perfect-forecast bound.** Each pump is its own meter (the
  placeholder), so a pump that runs for one hour in a month incurs its full demand charge. The
  GBM-driven plan ran PMP_21, PMP_22 and PMP_31 for 8–16 hours each to cover the high-demand forecast,
  and North's demand charge stayed at $3,865. The perfect-forecast plan never ran them, or PMP_32, and
  paid $2,479; its energy cost saving is only 0.2 points higher. How the pumps are metered changes this.
* **South depends on PMP_4.** The plan never runs PMP_4 or PMP_28, which operators ran for 632 and 539
  of June's 720 hours. With `[pumps] must_run = ["PMP_4"]` the MILP + GBM saving falls from 32.2% to 0.3%:
  energy cost is 1.3% above historical, and only a lower demand charge ($2,829 vs $2,997) keeps the bill
  under it. Must-run means every hour, a little stricter than the 88% of hours PMP_4 actually ran.
* **The forecast matters most for storage.** With the naive forecast the MILP still saves 7.2% and 21.8%,
  but North's simulated storage went under the reserve for 40 hours, 5 of them under the floor. With the
  GBM, 13 of North's 21 hours under the reserve fell on 1–4 June, after the surge described in section 5;
  the lowest point, 0.50 MG, was on 4 June.
* **The rule plan is a fair fallback.** With the GBM forecast it comes close to the MILP in South (29.1%)
  but makes three to four times as many starts.
* **Spilled water** (1.9 MG over 24 hours in North and 3.6 MG over 30 hours in South, for the MILP with
  the GBM) is pumping the plan didn't need: demand came in under the forecast while the tank was full.
  The simulated tank can't overfill, so that water counts as spilled and its energy is still billed.
  Live, the hourly re-plan cuts pumping sooner.
* **Results vary between runs.** Each MILP solve stops at its time limit, which 80 of North's 120
  GBM-driven solves and 46 of South's reached, so a plan depends on how fast the machine is and what else
  it is running. Given identical inputs, South's MILP with the naive forecast saved 29.8% in one run and
  21.8% in another: in the second, one more pump ran for at least an hour and added a $1,000 demand
  charge, while its kWh saving moved 0.16 points. North's MILP with the GBM saved 11.7% in a run before a
  small fix to gap filling and 8.6% after it, again through the demand charge ($3,471 vs $3,865). Read
  bill savings to within a few points and energy savings to within a fraction of one.

## 8. What to confirm before trusting the numbers

Values marked ASSUMPTION in `config/site.toml` are placeholders that let the code run on this data.

1. **The kW tag.** Reconcile it with a few months of utility bills. Until then, quote percentages, not
   dollars.
2. **Storage.** The usable volume of the tanks serving each section, which tanks serve which section,
   and the reserve, floor and maximum levels (fire flow and emergency storage requirements).
3. **Tariff.** Energy rates, on-peak hours and demand charge rules, including any ratchet, and which
   pumps share a meter (`[meters]`). With one meter per pump, pumps that run only a few hours a month
   set much of the demand charge.
4. **Operating limits.** Minimum run and rest times and starts per day, from motor and well specs.
5. **Must-run and out-of-service pumps**, especially PMP_4 and PMP_28, which South's plan stops using.
6. **Hydraulics.** Pressure zones and shared headers that make some pump combinations unusable.
7. **Water quality.** Blending, disinfection contact time, or rotating wells to keep water fresh.
8. **Time zone** of the export and the historian.

## 9. Next steps

* Add tank levels: they give true demand, a measured starting level for every plan, and a check on the
  storage model.
* Fit pump flow and power to the number of pumps running in each group, and refit them every few
  months.
* Add historical hourly weather (for example NOAA observations from the nearest station) as forecast
  features.
* Until the forecast handles the early-summer ramp, plan with a larger reserve from May to July, or
  widen the volume band, which covered 67–76% of days against 80% nominal.
* Count pump starts over a rolling 24 hours, including those made before the plan.
* Give the backtest's solver a node limit instead of a time limit, so results don't depend on machine
  speed.
* Check plans against a hydraulic model (EPANET) where pressure matters.
* Run in shadow mode for a few weeks: compare the recommendations with what operators did, and ask
  them why when they differ. Their reasons usually become constraints.
