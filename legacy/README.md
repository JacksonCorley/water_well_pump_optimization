# 2022 class-project code

These are the original CSE 6242 scripts, kept for reference. The maintained version is the `wellopt`
package and the dashboard in the repo root (`app.py`).

| File | What it did | Replaced by |
|---|---|---|
| `app.py` | Dash app: pick a date range and section, forecast demand, pick pumps | `app.py` + `wellopt/dashboard.py` (reads stored plans, what-if re-plan) |
| `forecast.py` | Prophet forecast of each section's hourly total flow (the app's choice; an ETS variant is also in the file), point forecast only | `wellopt/forecast.py` (hourly quantile GBM with calibrated bands, seasonal-naive baseline, rolling backtest) |
| `optimization.py` | cvxpy: cheapest pump set for each hour on its own | `wellopt/milp.py` + `wellopt/scheduler.py` (48 h schedule with storage, tariff, demand charges, start limits) |

The old optimizer chose pumps hour by hour from the hour's demand, so it ignored storage, switched
pumps on and off freely and used hindsight demand. The backtest keeps it as the `legacy_hourly`
policy for comparison; see `reports/backtest_summary.md`.

To run the old app, use its 2022 dependencies (pandas 1.2, prophet, cvxpy, the old Dash
component packages) in a separate environment, from the repo root so it finds the CSV files:

```bash
python legacy/app.py
```

Demo video of the 2022 app: https://youtu.be/H2ZkJvU6INA

## 2022 user guide

This is the guide from the original README.txt. The 2022 app used `Final_Data_hourly_clean.csv`.

1. **Date Range Selection**: select the start date and end date to get the forecast and optimized
   values. The date range is limited to the available data, so the application cannot go past 7/7/19
   or before 7/9/2016.
2. **Hour Selection**: the selected date range updates the hour selection field. You can get the
   forecast data in increments of 1 hour within the selected date range.
3. **Update Pump Projections**: when this button is pressed, flow is forecast for the selected date
   range and the optimization algorithm is run for every hour in the selected date range.
4. **Cumulative Power Usage**: the optimized cumulative power usage against the historical cumulative
   power usage for both north and south wells over the selected date range.
5. **North Section: Combined Active Pumps**: a line graph of the north section, with historical and
   forecast data for both flow and cumulative power usage over the selected date range.
6. **South Section: Combined Active Pumps**: the same for the south section.
7. **Well Operations Map**: pump locations and the optimized pump selection for the chosen hour.
8. **North Well Operations Table**: the north section's active pumps, their average flow and average
   power usage for the chosen hour.
9. **South Well Operations Table**: the same for the south section.
10. **Prediction Data**: all the historical flow, predicted flow, historical power usage and optimized
    power usage for both south and north wells.
