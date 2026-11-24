# Operations page

The operations page is one screen for the operator on shift. For each section it lists what the newest
plan says to do with every pump this hour, and charts where the chosen pump runs on its pump curve. It
reads the plan store that the planner writes, so start the planner first (`python -m wellopt live`, or
`python -m wellopt plan` for a single plan). It gives advice only. It never switches a pump.

## Running it

```bash
python ops_app.py
```

Then open http://127.0.0.1:8051. The planner, the dashboard and this page can run at the same time; they
share the plan store, `var/wellopt.sqlite` (`[live] store_path`).

| Use | Command |
|---|---|
| One user, on this computer | `python ops_app.py` |
| Another store or history file | `python -m wellopt.ops_page --store <file.sqlite> --history <export.csv>` (also `--config`, `--host`, `--port`, `--debug`) |
| Server, Linux | `gunicorn ops_app:server --bind 0.0.0.0:8001 --workers 1 --threads 4` |
| Server, Windows | `waitress-serve --listen=0.0.0.0:8001 ops_app:server` (`pip install waitress`) |

| Variable | Default | Meaning |
|---|---|---|
| `WELLOPT_CONFIG` | `config/site.toml` | Site config for `ops_app.py`. |
| `HOST`, `OPS_PORT` | `127.0.0.1`, `8051` | Address for `python ops_app.py`. |

The page loads nothing from the internet, so it works on a plant network without internet access. The
readings behind each plan come from the store's historian cache when the PI runner keeps it up to
date, and otherwise from the history CSV (`[site] history_csv`).

**The page has no login, and anyone who can open it can mark pumps out of service.** Serve it on the
plant network only, or behind the utility's reverse proxy with authentication and TLS. Marks are recorded
as made by "ops page". Each mark re-plans a section on the server, which usually takes a few seconds of
CPU (each solver stage stops at 10 seconds).

## The screen

The top bar has the North and South tabs, a Refresh button, the time the page last loaded and the hour
the plan starts. The page reloads by itself every 5 minutes, when it is shown again after a while in the
background, and straight away after a mark. If a reload fails, the page keeps the plan it has and says
when it was loaded. The address ends in `#North` or `#South`, so a bookmark or a wall screen can open on
one section.

It is drawn like a control-room screen: dark and grayscale, with cyan only for what needs doing and amber
only for what needs a look, as ISA-101 HMI screens are. Each section has its own color for the "now"
point and the tab: coral for North, light blue for South. Text is in IBM Plex Sans and Plex Mono, which
come with the page (`wellopt/ops_static/fonts/`, under the SIL Open Font License 1.1). It prints in black
on white.

### Cards

| Card | Shows |
|---|---|
| Demand | The median forecast for the plan's first hour, its P10–P90 band, and what the section produced in the last reading. |
| Running Now | The pumps running in the last reading, their total flow, and their kW from the pump table's averages (not a meter). |
| Planner Recommends | The pumps the plan runs this hour, their flow and kW, the change in kW from now, and which pumps to turn on and off. |
| Storage and Plan | Storage at the start of the plan, whether it was measured or assumed (and from where), and the plan's status and age. A ▲ line appears when the plan is older than `[live] stale_after_minutes`. |

### Pump actions

The list on the left has one row per pump in the section, grouped under "Do this hour" (turn on and turn
off), "Keep running", "Leave off" and "Out of service", each with a count, in this order:

| Action | Meaning |
|---|---|
| ▲ Turn on | The plan starts it this hour. |
| ▼ Turn off | The plan stops it this hour, or it is out of service but still running. |
| ● Keep running | It is running, and the plan keeps it on. |
| ○ Leave off | It is off, and it isn't needed this hour. |
| ✕ Out of service | Marked on this page, listed in `[pumps] out_of_service`, or without the readings the planner needs. |

Rows to do this hour are in bright text, with Turn on as a filled cyan label and Turn off as an outlined
one. Flow and Head come from the last reading before the plan starts, and the head is an estimate (see
[Limits](#limits)). kWh/kgal is the pump's energy per thousand gallons from its history, which sets its
place in the merit order. Check is the [operating check](#the-operating-check): ✓ and gray when it finds
nothing, amber with a ▲ when it flags something. Lines marked ▲ above the list report problems, such as
a running pump with no usable reading or a curve file that couldn't be read.

### Pump curve

Click a pump to chart it, or move through the list with ↑ and ↓ (Home and End jump to the ends). The
line above the chart gives its action, the reason, and when the plan next switches it (for example
"Planned: on until Tue 14:00."). Flow is across, estimated total dynamic head is up. Point at the chart,
or touch it, to read the maker curve, the fitted curve and the % of best-efficiency flow at that flow;
near a marked point it gives that point's figures instead.

| In the legend | Where it comes from |
|---|---|
| Preferred range (70–120% of BEP), the shaded band | The maker curve's efficiencies |
| Running hours since …, the grey dots | Up to 300 of its running hours in the history window (`[live] lookback_days`) |
| Usual range (middle 90%), the dashed box | The 5th to 95th percentile of its flow, and of its head, while running |
| Maker curve, and the star for the best efficiency point | `config/pump_curves.csv` |
| Fitted to its history (R² …), the dotted line | H = h0 + kQ² fitted to its running hours. Drawn only if head falls with flow and R² is at least `fit_min_r2` |
| Now (HH:MM reading), the dot in the section's color | The last reading, if the pump is running |
| Expected if started, the open circle | Its median running point, for a pump the plan turns on |

The notes under the chart say in words what the check found, where the curve came from, and how the head
was estimated for that pump. Below them are the reason box and the out-of-service button.

### Planner notes

The plan's own notes, the same as on the dashboard's Live plan tab.

## The operating check

The Check column compares a running pump's last reading with its maker curve, its best efficiency flow
and its own history. It shows the first two flags it finds, or "Usual" when there are none.

| Compared with | Flag, for example | What it usually means |
|---|---|---|
| The maker curve | Head 12% below curve | Wear, or a static level on file that is out of date. Flagged beyond `head_off_curve_pct`. |
| | Head 13% above curve | A static level or specific capacity on file that needs checking, or the wrong curve. |
| | Off the curve | Flow outside the range the maker's points cover. |
| Best efficiency flow | 59% of BEP | Below the preferred range (`por_low_pct`): wear through recirculation and vibration. |
| | 131% of BEP | Above it (`por_high_pct`): risk of cavitation and of overloading the motor. |
| Its own history | Flow high, Head low, … | Outside the middle 90% of its running hours. |

"No reading" means the pump is running but that hour has no usable flow and pressure. Without a maker
curve only the history check runs; when a fitted curve is drawn, the notes say how far the point sits
from it.

## The pump curves file

`config/pump_curves.csv` (`[pump_curves] path`) holds the maker's curve for each pump, one row per point
read off the curve sheet or the pump test. It ships with only the header row. These numbers are made up,
to show the layout:

```csv
pump,flow_gpm,head_ft,efficiency_pct,source
PMP_13,400,318,38,made-up example
PMP_13,800,300,60,made-up example
PMP_13,1200,272,74,made-up example
PMP_13,1500,248,78,made-up example
PMP_13,1800,218,75,made-up example
PMP_13,2100,180,66,made-up example
```

* `pump` is the pump ID used in `Well_Summarys.csv`, such as `PMP_13`.
* Give at least 3 points per pump, spread across the curve, each at a different flow. More points near
  the best efficiency point place it better.
* `efficiency_pct` is optional, but without it there is no best efficiency point and no preferred range.
  The page takes the point with the highest efficiency and refines it with a parabola through that point
  and its two neighbours.
* `source` is optional. Say where the curve came from, for example "pump test 2018" or the maker's curve
  number. It is shown under the chart.
* The curve runs straight between points and stops at the first and last point.
* Rows can be in any order. Blank lines are skipped.

In Excel, save it as "CSV UTF-8" or plain "CSV"; the page reads both. A number shown as 1,200 is fine,
because Excel puts quotes around it. If you type the file by hand, write 1200, and put double quotes
around any text that has a comma.

The page reads the file again when it changes, so Refresh shows new curves without a restart. If any row
is wrong, no curves are shown and the warning names the line to fix, for example "line 5: head_ft must be
positive". A typo is reported instead of being drawn as a wrong curve. Line 1 is the header, as in Excel's
row numbers. If the file can't be read (Excel can lock it while it is open), the page says so and tries
again at the next refresh.

## Out of service

To take a pump out of service, click it in the list, type a reason (optional, up to 200 characters) and
click **Mark out of service**. The button reads "Saving…" while the section is planned again, which can
take up to a minute, and the result is shown under it. Then:

1. The mark is saved in the plan store's `pump_marks` table with the reason and the time.
2. An info alert `out_of_service:<pump>` is added, so the change shows in the dashboard's alert list.
   Info alerts aren't sent to the webhook.
3. The section is planned again without the pump: the same hours, forecast and starting readings. The
   new plan is saved as the section's newest plan (run source `ops-page`), so the dashboard shows it too.
4. Every later planning run leaves the pump out until it is returned to service.

To bring it back, click it and click **Return to service**. The reason box shows the reason it was
marked with. The section is planned again with it.

If a marked pump is still running, it is listed as ▼ Turn off with "It is running now: turn it off." If
the re-plan fails, the mark is still saved, and the message says the next planning run will use it.

Pumps listed in `[pumps] out_of_service` in `config/site.toml` are for long outages. They show as
✕ Out of service, and the button reads "Not available to plan"; change them in the config. So do pumps
whose history has no usable flow and power readings.

## Settings

`[pump_curves]` in `config/site.toml`:

| Setting | Default | Meaning |
|---|---|---|
| `path` | `config/pump_curves.csv` | The pump curves file. |
| `por_low_pct`, `por_high_pct` | 70, 120 | Preferred operating range, as % of best-efficiency flow. Use the maker's range where it gives one. |
| `head_off_curve_pct` | 10 | Flag head this far above or below the maker curve, in %. |
| `fit_min_r2` | 0.45 | Draw a curve fitted to the history only if head falls with flow and it fits this well. |

The page also uses `[live] store_path`, `lookback_days` and `stale_after_minutes`.

## How it is built

The page is plain HTML, CSS and JavaScript in `wellopt/ops_static/`, with no libraries and no build step.
`wellopt/ops_page.py` is a small Flask app that serves it, with two JSON routes around the plan store:

| Route | Does |
|---|---|
| `GET /api/views` | The newest plan per section as the page shows it, with each plan's age and each pump's chart frame |
| `POST /api/marks` | Marks a pump out of service, or returns it, and re-plans its section. The body is `{"pump": "PMP_3", "out_of_service": true, "note": "bearing noise"}` |

Marks are taken only as JSON, which a form on another website cannot send, and the page refuses requests
a browser labels as coming from another site. The page only runs its own script and shows notes as plain
text. These checks are not a login: anyone who can open the page can still mark pumps.

## Limits

* **No head is measured.** Head is estimated as the static level, plus flow ÷ specific capacity
  (drawdown), plus discharge pressure × 2.31 ft/psi, using the values in `Well_Summarys.csv`. Static levels
  change with the seasons and with drought. A pump that looks off its curve may have a static level on
  file that is out of date, rather than a fault. The notes under each chart give the figures used.
* **There are no maker curves on file yet.** Until `config/pump_curves.csv` is filled in, the chart shows
  history only: no maker curve, best efficiency point or preferred range, and the Check column compares
  only with the pump's own history.
* **Most pumps ran close to one operating point in 2016–2019**, so their grey dots form a small cluster,
  and a curve fitted to the history rarely passes `fit_min_r2`.
* **Some histories show head rising with flow**, which a fixed-speed pump cannot do. The page says so
  under the chart. The usual causes are a variable-speed drive, or a static level or specific capacity on
  file that needs checking.
* **kW and kWh/kgal come from the pump table**, which uses the recorded kW tag by default. That tag
  records less power than physics allows; a physics estimate is about 7 times the recorded value (see
  [METHODOLOGY section 2](METHODOLOGY.md#2-the-energy-data-fails-a-physics-check)). The figures are fair
  for comparing pumps, less so in the South, but they are not metered energy. With
  `[power_model] source = "physics"` they come from the physics model instead.
* **The page is only as current as the newest plan.** When the planner stops, the plan's age in the
  Storage and Plan card grows, and the card warns once it passes `stale_after_minutes`.
