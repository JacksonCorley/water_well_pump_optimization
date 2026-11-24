Water Well Pump Optimization Application

DESCRIPTION:
An advisory scheduler for a well field. It forecasts each section's water demand for the next 48 hours,
plans which wells to run so the tanks stay within their limits at the lowest electricity cost, and shows
the plan on an operator dashboard. An operations page lists what to do with each pump this hour and
shows where each pump runs on its pump curve. Operators decide what to run.

readme.md has the full guide, results and commands. docs/METHODOLOGY.md explains the method and its
assumptions; docs/LIVE_DEPLOYMENT.md covers running it against a PI historian; docs/OPS_PAGE.md covers
the operations page and the pump curve file.

INSTALLATION (Docker):
1) Install Docker Desktop from https://www.docker.com/ and start it.
2) Open a terminal in the water_well_pump_optimization folder.

EXECUTION:
1) Start the dashboard:
    docker compose up --build
2) Open http://localhost:8000/
3) Optional: make a plan from the end of the history, then refresh the page:
    docker compose run --rm tasks plan
4) With a plan, the operations page is on http://localhost:8001/

INSTALLATION (Python 3.12 or newer, no Docker):
    python -m venv .venv
    .venv\Scripts\activate          (Windows; on macOS and Linux: source .venv/bin/activate)
    pip install -r requirements.txt
    python -m wellopt plan
    python app.py                   (then open http://127.0.0.1:8050/)
    python ops_app.py               (operations page: http://127.0.0.1:8051/)

DEMO VIDEO (2022 version): https://youtu.be/H2ZkJvU6INA

The 2022 class-project app and its user guide are in legacy/.

Folder structure:
    --app.py                    -> dashboard entry point (Plotly Dash)
    --ops_app.py                -> operations page entry point (Flask; the page is in wellopt/ops_static/)
    --wellopt/                  -> the package: data QC, pump models, tariff, forecasts, scheduler,
                                   backtest, reports, dashboard, and the live planner (wellopt/live/)
    --config/site.toml          -> site settings; values marked ASSUMPTION need the utility's real numbers
    --config/pump_curves.csv    -> maker pump curves for the operations page (to be filled in)
    --assets/style.css          -> dashboard styles
    --reports/                  -> data audit and backtest reports
    --docs/                     -> methodology, live deployment and operations page notes
    --tests/                    -> pytest suite
    --legacy/                   -> the 2022 scripts (app.py, forecast.py, optimization.py)
    --Dockerfile, docker-compose.yml
    --requirements.txt          -> pinned Python packages (requirements-dev.txt adds pytest)
    --Final_Data_hourly.csv     -> hourly SCADA export, July 2016 to July 2019
    --Well_Summarys.csv         -> list of all the pumps, their location, and their specs
