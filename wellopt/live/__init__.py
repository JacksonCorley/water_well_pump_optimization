"""Live advisory runner.

Every cycle (hourly by default):

1. ingest  - read new hourly rows from a source (CSV replay or a PI Web API historian);
2. QC      - the same rules as the offline analysis (`wellopt.data.quality_check`);
3. guard   - skip planning for a section whose data is stale, and raise an alert;
4. forecast- load (or retrain) the demand forecaster; fall back to seasonal-naive if it fails;
5. plan    - storage-aware MILP over 48 h, falling back to the merit-order rule;
6. publish - write plans, runs and alerts to SQLite for the app; post alerts to a webhook.

The runner is advisory: it never writes to SCADA. Operators see the plan in the app.
"""
