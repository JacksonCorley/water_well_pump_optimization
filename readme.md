# Water well pump optimization

An advisory scheduler for a well field. It forecasts each section's water demand for the next 48 hours,
plans which wells to run so the tanks stay within their limits at the lowest electricity cost (time-of-use
energy, demand charges and pump starts), and shows the plan on an operator dashboard. It recommends; it
never switches pumps.

It began as a 2022 CSE 6242 class project ([demo video](https://youtu.be/H2ZkJvU6INA)). That version is
kept in [`legacy/`](legacy/README.md). This one rebuilds the method, tests it against what actually
happened, and adds a live mode.

| | 2022 version | Now |
|---|---|---|
| Data | Used as exported | Quality control, plus an energy audit that found the kW tag reads about 7× below physics |
| Forecast | Prophet point forecast of section flow | Hourly quantile gradient boosting with calibrated P10–P90 bands, checked against a seasonal-naive baseline |
| Optimizer | Cheapest pump set each hour on its own, with hindsight | 48-hour MILP with storage, time-of-use prices, demand charges, minimum run and rest times and start limits, with a rule-based fallback |
| Evaluation | None | Closed-loop backtest that replans every 6 hours against actual demand |
| Operation | Dashboard over a chosen date range | Hourly advisory loop (CSV replay or PI Web API), plan store, alerts, and a dashboard with what-if re-planning |

## Results

A closed-loop backtest over June 2019 ([reports/backtest_summary.md](reports/backtest_summary.md)) replays
the month as if the scheduler had been running: it plans 48 hours ahead with only the data it would have
had, applies the first 6 hours against actual demand, and plans again. Against what operators did, the
MILP scheduler with the GBM forecast gives:

| | North | South |
|---|---|---|
| Electricity bill (storage-adjusted) | 8.6% lower | 32.2% lower |
| Energy, with recorded kW / physics power model | 11.9% / 7.5% lower | 32.0% / 22.8% lower |
| Demand charge | $3,865, unchanged | $2,000 vs $2,997 |
| Pump starts | 102 vs 370 | 63 vs 296 |
| Lowest simulated storage | 0.50 MG, under the reserve for 21 h | 1.05 MG, never under the reserve |
| With a perfect demand forecast (a bound) | 18.7% lower | 34.5% lower |

* **South's saving rests on PMP_4.** The plan never runs PMP_4, which operators ran 88% of June's hours at
  about 2.7× the energy per gallon of the next-worst South pump. With PMP_4 set to must-run, South's
  saving falls to 0.3%. Find out why it runs before acting on this.
* **North's gap to the bound is demand charges.** With one meter per pump (a placeholder), a pump run for
  a single hour incurs its whole monthly demand charge. The plan ran three pumps for 8–16 hours each to
  cover the high-demand forecast; the perfect-forecast plan never ran them.
* **The 2022 method isn't runnable.** With hindsight it shows 7.6% (North) and 29.6% (South), but it
  starts pumps 864 and 947 times in the month, up to 8 times per pump per day.
* **The GBM beats the seasonal-naive forecast hour by hour** (about 8% lower error in both sections) but
  not on day-ahead volume: it under-forecasts when demand climbs above anything in its training data, as
  it did in May 2019. Weather features are the next step.
* **Solver time limits make bill savings vary between runs.** Given identical inputs, South's MILP with
  the naive forecast saved 29.8% in one run and 21.8% in another, because in the second one more pump ran
  for at least an hour and set a $1,000 demand charge. The MILP's energy savings moved by under 0.2 points.

**Read these numbers with two caveats.** The recorded kW fails a physics check for all 28 pumps, so
dollar and kWh figures are indicative; percent savings against historical operation hold up better.
And the export has no tank levels, so demand is taken to be production and the tank sizes, tariff and
operating limits in `config/site.toml` are placeholders. [docs/METHODOLOGY.md](docs/METHODOLOGY.md)
explains both and lists what to confirm with the utility.

## Quick start

Python 3.12 or newer (tested on 3.13). From the repo root:

```bash
python -m venv .venv
```

Activate it with `.venv\Scripts\activate` on Windows or `source .venv/bin/activate` on macOS and Linux,
then install the dependencies:

```bash
pip install -r requirements.txt
```

Make a plan from the end of the history (the first run trains the demand models, a minute or two):

```bash
python -m wellopt plan
```

Start the dashboard on http://127.0.0.1:8050:

```bash
python app.py
```

Start the operations page on http://127.0.0.1:8051:

```bash
python ops_app.py
```

### With Docker

```bash
docker compose up --build
```

The dashboard is on http://localhost:8000 and shows the committed backtest and audit reports. The
operations page is on http://localhost:8001 and needs a plan. To add one:

```bash
docker compose run --rm tasks plan
```

To run the planner loop as well (it replays the CSV history unless configured for a historian):

```bash
docker compose --profile live up -d
```

## Commands

Every command takes `--help`.

| Command | What it does | Writes |
|---|---|---|
| `python -m wellopt audit` | Data quality and the energy physics check | `reports/data_audit.md` |
| `python -m wellopt forecast-eval` | Rolling-origin forecast backtest over the last 182 days (about 6 minutes) | `reports/forecast_eval.md` |
| `python -m wellopt backtest` | Closed-loop policy comparison over the last whole month (about 15 minutes with `--jobs 4`, which runs policies in parallel) | `reports/backtest_summary.md` |
| `python -m wellopt train` | Fit the demand models on all history | `models/` |
| `python -m wellopt plan` | Plan both sections from the end of the history, or from `--at` | `var/wellopt.sqlite` |
| `python -m wellopt live` | Advisory loop: `--source csv` replays the export, `--source pi` reads a PI historian | `var/wellopt.sqlite` |

## Dashboard

* **Live plan**: each section's status, alerts and notes, the demand forecast band, expected storage, the
  48-hour pump schedule and a schematic map of running wells. A what-if panel re-plans with a different
  storage level or extra pumps out of service, without saving.
* **Backtest**: savings by policy, cumulative cost and simulated storage over the backtest month.
* **Data audit**: the energy check and each section's merit order.

Every chart has a table view.

## Operations page

`python ops_app.py` is one screen for the operator on shift, in a control-room style: grayscale, with
color only for what needs doing and what needs a look. It lists what
the newest plan says to do with each pump this hour (turn on, turn off, keep running, leave off), and
charts where the chosen pump runs on its pump curve: its reading now, its usual range from the history
and, once `config/pump_curves.csv` holds the maker's curves, the curve, best efficiency point and
preferred range. A pump marked out of service there is left out of a new plan at once, and of every
later plan until it is returned to service. [docs/OPS_PAGE.md](docs/OPS_PAGE.md) covers the page, the
curve file and the page's limits.

## Configuration

Everything site-specific lives in [`config/site.toml`](config/site.toml): tank volumes and limits,
tariff, operating rules, out-of-service and must-run pumps, forecast settings and the live-mode
settings. Set `WELLOPT_CONFIG` to use another file. Running it against a real historian is covered in
[docs/LIVE_DEPLOYMENT.md](docs/LIVE_DEPLOYMENT.md).

`[forecast] target` picks what the GBM predicts: flow in gpm (`"absolute"`, the default) or flow as a
multiple of the past week's mean (`"ratio"`). The ratio has the lower average error on this data but
handled a sudden surge worse; [METHODOLOGY section 5](docs/METHODOLOGY.md#5-demand-forecast) compares them.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

## Repository layout

| Path | Contents |
|---|---|
| `app.py` | Dashboard entry point (`python app.py`, or `gunicorn app:server`) |
| `ops_app.py` | Operations page entry point (`python ops_app.py`, or `gunicorn ops_app:server`) |
| `wellopt/data.py` | Load the SCADA export; quality control |
| `wellopt/pumps.py` | Pump flow and power models; the physics check |
| `wellopt/tariff.py` | Time-of-use energy and demand charges |
| `wellopt/forecast.py` | Seasonal-naive and quantile GBM forecasters; rolling evaluation |
| `wellopt/milp.py`, `wellopt/scheduler.py` | The MILP, the rule-based fallback, and plans with notes |
| `wellopt/backtest.py`, `wellopt/reporting.py`, `wellopt/diagnostics.py` | Backtest and reports |
| `wellopt/dashboard.py`, `assets/style.css` | The Dash app |
| `wellopt/curves.py`, `wellopt/ops_view.py`, `wellopt/ops_page.py`, `wellopt/ops_static/` | Pump curves and operating checks; the operations page (a Flask app serving a plain HTML, CSS and JavaScript page) |
| `wellopt/live/` | Planner loop, data sources (CSV replay, PI Web API), SQLite store, saved models, weather |
| `config/` | Site config, the maker pump curves (`pump_curves.csv`) and an example PI tag map |
| `reports/` | Generated reports, committed so the results are visible |
| `docs/` | Methodology, deployment and operations page notes |
| `tests/` | pytest suite |
| `legacy/` | The 2022 scripts |
| `Final_Data_hourly.csv` | Hourly SCADA export, July 2016 to July 2019 |
| `Well_Summarys.csv` | Pump list, locations and specs |
