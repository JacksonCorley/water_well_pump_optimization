# Data audit: Well system (historical replay)

Generated 2026-11-14T01:13:13 from hourly data 2016-07-07 01:00:00 to 2019-07-07 23:00:00.

## 1. Energy data fails a physics check

**FAIL: recorded kW is below the physical minimum for most pumps; check units and aggregation against utility bills before quoting kWh or $.**

Lifting water takes at least flow x head / 3960 hp. Using the discharge pressure alone (no well lift) and a perfect motor and pump, 28 of 28 pumps record *less* power than that minimum (median implied efficiency 263%). Real wire-to-water efficiency is 50-75%.

The recorded kW still tracks flow x pressure closely (median R² 0.97), and the physics estimate is a fairly steady 7.0x the recorded value (10th-90th percentile 6.3x-8.1x). That pattern points to a scaling or aggregation error in the kW tag, not random noise.

Consequences:

* Absolute kWh and $ figures from this data (including the original project's savings claims) cannot be trusted until the tag is reconciled with utility bills.
* Rankings are more robust: the rank correlation of kWh/kgal under recorded vs physics power is North 0.93, South 0.73. Savings measured *relative* to historical operation are therefore more meaningful than dollars.
* Set `[power_model] source = "physics"` in `config/site.toml` to plan with the physics model instead.

## 2. Coverage and rejected readings

* 26303 hourly slots, 93 missing from the export.
* Spring daylight-saving gaps: 2017-03-12 03:00:00, 2018-03-11 03:00:00, 2019-03-10 03:00:00.
* Gaps over 2 hours: 2018-04-03 22:00:00 -> 2018-04-05 08:00:00, 2018-10-30 00:00:00 -> 2018-11-01 10:00:00.
* North: 26175 valid production hours; 35 hours with a rejected pump reading; 0 all-zero hours; 0 above capacity; 128 hours imputed for model inputs (never scored).
* South: 26181 valid production hours; 29 hours with a rejected pump reading; 0 all-zero hours; 0 above capacity; 122 hours imputed for model inputs (never scored).

Rejected readings by pump (flow out of range, kW out of range, negative pressure while running, flow without kW):

| pump | bad_flow | bad_kw | bad_press | flow_without_kw |
|---|---|---|---|---|
| PMP_18 | 1 | 0 | 0 | 0 |
| PMP_3 | 0 | 0 | 22 | 0 |
| PMP_1 | 0 | 0 | 29 | 0 |
| PMP_27 | 0 | 0 | 187 | 0 |
| PMP_23 | 0 | 0 | 5 | 0 |
| PMP_19 | 1 | 1 | 0 | 0 |
| PMP_8 | 0 | 0 | 1 | 0 |
| PMP_13 | 0 | 0 | 2 | 0 |
| PMP_28 | 1 | 1 | 0 | 0 |
| PMP_12 | 1 | 1 | 0 | 0 |
| PMP_11 | 0 | 0 | 7 | 0 |
| PMP_22 | 0 | 0 | 9 | 0 |

## 3. Pump table

Planning flow and power are means over valid running hours. `implied_eff` uses discharge pressure only, so real efficiency would be even higher than shown: anything above 1.0 is impossible.

### North (cheapest first)

| pump | group | flow_gpm | kw_recorded | kw_physics | implied_efficiency | physics_to_recorded | kwh_per_kgal | duty | energy_share |
|---|---|---|---|---|---|---|---|---|---|
| PMP_9 | N2 | 936.211 | 5.677 | 34.284 | 1.402 | 6.039 | 0.101 | 0.406 | 0.016 |
| PMP_23 | N2 | 1940.043 | 16.512 | 135.693 | 4.163 | 8.218 | 0.142 | 0.395 | 0.046 |
| PMP_6 | N3 | 1969.499 | 19.265 | 136.778 | 2.765 | 7.100 | 0.163 | 0.519 | 0.071 |
| PMP_1 | N3 | 2344.564 | 23.839 | 172.301 | 2.925 | 7.228 | 0.170 | 0.455 | 0.077 |
| PMP_24 | N3 | 892.998 | 9.173 | 65.221 | 2.779 | 7.110 | 0.171 | 0.438 | 0.029 |
| PMP_11 | N3 | 1764.297 | 18.392 | 133.045 | 2.932 | 7.234 | 0.174 | 0.264 | 0.035 |
| PMP_2 | N1 | 1880.734 | 19.931 | 135.000 | 2.363 | 6.774 | 0.177 | 0.048 | 0.007 |
| PMP_19 | N3 | 1843.262 | 19.746 | 139.373 | 2.742 | 7.058 | 0.178 | 0.440 | 0.062 |
| PMP_8 | N1 | 2124.404 | 23.527 | 157.717 | 2.276 | 6.704 | 0.185 | 0.517 | 0.086 |
| PMP_32 | N2 | 2641.077 | 29.447 | 197.191 | 2.270 | 6.697 | 0.186 | 0.667 | 0.139 |
| PMP_27 | N3 | 1900.470 | 22.779 | 158.083 | 2.570 | 6.940 | 0.200 | 0.200 | 0.034 |
| PMP_31 | N3 | 1883.887 | 23.304 | 154.725 | 2.213 | 6.639 | 0.206 | 0.454 | 0.075 |
| PMP_10 | N1 | 2146.216 | 27.716 | 185.243 | 2.256 | 6.684 | 0.215 | 0.547 | 0.108 |
| PMP_22 | N1 | 2236.323 | 29.951 | 193.891 | 2.002 | 6.474 | 0.223 | 0.601 | 0.128 |
| PMP_21 | N1 | 1747.428 | 32.806 | 199.961 | 1.531 | 6.095 | 0.313 | 0.375 | 0.087 |

Largest energy user: PMP_32 (14% of North pump energy, 0.186 kWh/kgal, runs 67% of hours).

### South (cheapest first)

| pump | group | flow_gpm | kw_recorded | kw_physics | implied_efficiency | physics_to_recorded | kwh_per_kgal | duty | energy_share |
|---|---|---|---|---|---|---|---|---|---|
| PMP_30 | S5 | 1442.326 | 9.847 | 81.899 | 4.279 | 8.318 | 0.114 | 0.401 | 0.029 |
| PMP_3 | S1 | 959.658 | 6.563 | 56.974 | 4.715 | 8.681 | 0.114 | 0.324 | 0.016 |
| PMP_25 | S3 | 2138.721 | 17.313 | 127.643 | 3.103 | 7.373 | 0.135 | 0.569 | 0.073 |
| PMP_16 | S4 | 1730.816 | 14.467 | 115.484 | 3.857 | 7.983 | 0.139 | 0.417 | 0.045 |
| PMP_18 | S3 | 2214.344 | 18.961 | 133.315 | 2.685 | 7.031 | 0.143 | 0.263 | 0.037 |
| PMP_7 | S2 | 2421.357 | 21.028 | 140.392 | 2.251 | 6.676 | 0.145 | 0.634 | 0.099 |
| PMP_13 | S1 | 1348.782 | 12.413 | 97.545 | 3.702 | 7.859 | 0.153 | 0.458 | 0.042 |
| PMP_15 | S3 | 1514.511 | 13.998 | 99.379 | 2.764 | 7.099 | 0.154 | 0.515 | 0.053 |
| PMP_14 | S4 | 1417.722 | 13.995 | 104.156 | 3.209 | 7.442 | 0.165 | 0.076 | 0.008 |
| PMP_29 | S3 | 1807.542 | 17.904 | 124.229 | 2.567 | 6.939 | 0.165 | 0.454 | 0.060 |
| PMP_28 | S5 | 1946.359 | 19.901 | 136.999 | 2.518 | 6.884 | 0.170 | 0.702 | 0.104 |
| PMP_12 | S5 | 1963.459 | 20.186 | 128.028 | 1.832 | 6.343 | 0.171 | 0.297 | 0.044 |
| PMP_4 | S2 | 3027.049 | 83.341 | 472.010 | 1.037 | 5.664 | 0.459 | 0.629 | 0.389 |

Largest energy user: PMP_4 (39% of South pump energy, 0.459 kWh/kgal, runs 63% of hours).

Unavailable (no data or out of service): PMP_5, PMP_26, PMP_20.

## 4. Assumptions to confirm

* North usable storage 3.50 MG (min 30%, floor 10%, max 95%). No tank telemetry in the export, so demand = production and storage is simulated.
* South usable storage 3.20 MG (min 30%, floor 10%, max 95%). No tank telemetry in the export, so demand = production and storage is simulated.
* Tariff: 0.070 $/kWh off-peak, 0.140 $/kWh on-peak 14:00-20:00 weekdays, demand charge 12.00 $/kW-month, one meter per pump.
* Pump rules: min up 4 h, min down 2 h, max 2 starts per pump per day, $10 per start.
* Power model for planning: recorded kW (physics model assumes 70% wire-to-water efficiency).
* Timestamps are local wall clock (America/Chicago); the spring daylight-saving hour is missing.
* Out of service: PMP_5, PMP_20, PMP_26. Must run: none.
