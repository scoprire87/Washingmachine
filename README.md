# Appliance Cycle

*[Italiano](README.it.md)*

A Home Assistant integration that turns any **power sensor** (smart plug,
energy meter, ...) into a cycle tracker for a dishwasher, washing machine or
dryer:

- detects when a cycle **starts** and **ends**;
- **learns the programs** from the power profile and recognises them the next
  time they run;
- shows the **remaining time**, the **estimated end** and the **progress**;
- sends a **notification** when the cycle ends (and optionally a few minutes
  before).

No cloud, no appliance API: only the power readings already in Home Assistant.

## Installation

### HACS

1. HACS → ⋮ → *Custom repositories* → add `https://github.com/scoprire87/washingmachine`
   as *Integration*.
2. Install **Appliance Cycle** and restart Home Assistant.

### Manual

Copy `custom_components/appliance_cycle` into the `custom_components` folder
of your configuration and restart Home Assistant.

## Configuration

*Settings → Devices & services → Add integration → Appliance Cycle*

| Field | Description |
| --- | --- |
| Name | e.g. `Dishwasher` |
| Power sensor | a `sensor` with device class *power* (W or kW) |
| Appliance | dishwasher, washing machine, dryer or other: sets sensible detection thresholds |

Then open **Configure** on the entry. The menu offers:

- **Notifications and detection** (below);
- **Rename a program**, **Correct the program of the last cycle**,
  **Delete programs** (one or more at once);
- **Program durations (display)**: one program per line with the duration the
  appliance display shows, for example `Intenso 3:16`. When the running cycle
  is recognised as one of them, the remaining time counts down from that
  duration, like the display;
- **Load the preset programs** (dishwasher): programs recorded from a real
  dishwasher, useful if you have the same model. Learned programs with an
  automatic name that match one of them take its name;
- **Learn from a CSV log**: upload a CSV downloaded from the history of the
  power sensor (*History → ⋮ → Download data*), also from the phone.

Notifications and detection:

| Option | Description |
| --- | --- |
| Notify | one or more notify services (`notify.mobile_app_...`, `notify.telegram`, `persistent_notification.create`, ...) |
| Notify before the end | send a reminder N minutes before the estimated end (0 = off) |
| Keep "finished" for | minutes the status stays *finished* before going back to *idle* |
| Detection (advanced) | idle power, start power and end delay |

The sensor should report at least every 10–30 seconds while the appliance is
running; most smart plugs do.

## Entities

| Entity | Description |
| --- | --- |
| `binary_sensor.<name>_running` | on while a cycle is running |
| `sensor.<name>_status` | `idle`, `running`, `finished`; attributes `started`, `last_cycle` |
| `sensor.<name>_program` | recognised program; attributes `confidence`, `known_programs` |
| `sensor.<name>_remaining_time` | minutes to the end |
| `sensor.<name>_estimated_end` | timestamp of the estimated end |
| `sensor.<name>_progress` | % of the cycle |
| `sensor.<name>_cycle_energy` | kWh used by the current / last cycle |

## How it works

**Detection.** A cycle starts when the power goes above the *start power* and
ends when it stays at or below the *idle power* for the *end delay*. Short
activity that never reaches the start power (display, door auto-opening) is
ignored.

For dishwashers the default idle power is 1 W: after the last heating the
program goes on for up to ~2 hours of drying (fan ~15 W, then readings
alternating between 0 and 2 W) and ends when the display says so. With a
higher threshold (e.g. 4 W) the drying is left out of the cycle.

**Learning.** Every finished cycle is stored as a power profile. Cycles with
the same profile are grouped into one program (`Program 1`, `Program 2`, ...).
Give them meaningful names from **Configure**.

When a cycle matches no known program, besides the "finished" notification you
are asked **"which program was it?"**: mobile app notifications
(`notify.mobile_app_...`) have a *Reply* button, just type the name (e.g.
`intenso`; it is written like the display durations entry). The question also
appears in the Home Assistant notifications and the
`appliance_cycle_unknown_program` event is fired.

If a cycle lasts longer than the stored runs (for example runs stored before the
drying was included) but within 15% of the display duration of the program
recognised while it ran, it gets that program.

**Remaining time.** While a cycle runs, its profile so far is aligned (dynamic
time warping) with the learned runs: the best match gives the program and how
far into it the cycle is. While the cycle does not look like any learned
program (a program never run before), the remaining time and the program stay
*unknown* rather than showing the estimate of a different program; the new
program is learned when the cycle ends.

Tested on real dishwasher traces (9 programs, leave-one-out): the program is
recognised from about half of the cycle and the estimated end is typically
within 2–4 minutes at mid-cycle, within 2 minutes in the last quarter.
Programs that start the same way (e.g. *Quick* and *Complete*) can be confused
in the first minutes; the estimate corrects itself as soon as they differ.

## Getting started faster

If the power sensor is already recorded in Home Assistant, learn the past
cycles at once:

```yaml
action: appliance_cycle.import_history
data:
  entity_id: sensor.dishwasher_status
  days: 10
```

If you have logs exported from the history (*History → ⋮ → Download data*,
one CSV per program), upload them from **Configure → Learn from a CSV log**
with the name of the program, or copy them in the configuration folder and use
the service:

```yaml
action: appliance_cycle.import_file
data:
  entity_id: sensor.dishwasher_status
  file: dishwasher/eco.csv
  program: Eco
```

Then rename the programs from **Configure → Rename a program**, or with the
service:

```yaml
action: appliance_cycle.rename_program
data:
  entity_id: sensor.dishwasher_status
  program: Program 1
  new_name: Eco
```

## Services

| Service | Description |
| --- | --- |
| `import_history` | learns the cycles found in the recorder history (`days`, default 10); returns `{cycles: n}` |
| `import_file` | learns the cycles of a CSV exported from the history (`file` relative to the configuration folder, optional `program` name); returns `{cycles: n}` |
| `rename_program` | renames a learned program |
| `delete_program` | forgets a program |
| `set_last_program` | the last cycle was recognised wrongly: assign it to another (new or existing) program |
| `set_program_durations` | display durations per program (`programs`: `{"Intenso": "3:16"}` or text with one `Intenso 3:16` per line); returns `{programs: n}` |
| `archive_runs` | archive the stored runs: they still help recognising programs but no longer count in the average durations once newer runs exist; returns `{runs: n}` |

## Events and automations

`appliance_cycle_started`, `appliance_cycle_finished` and
`appliance_cycle_unknown_program` (a cycle of a program never seen before:
`program` is the automatic name, `duration` in seconds) are fired on the bus.
The *finished* event carries `name`, `program`, `started`, `ended`,
`duration` (s) and `energy` (Wh).

```yaml
triggers:
  - trigger: state
    entity_id: sensor.dishwasher_status
    to: finished
actions:
  - action: light.turn_on
    target:
      entity_id: light.kitchen
    data:
      flash: short
```

## Tips

- After a restart a running cycle is resumed from the recorder history
  (last 8 hours).
- If cycles end too early (long pauses, soaking), increase the *end delay*.
- If the standby or display consumption triggers cycles, raise the *start
  power*; if the end is never detected, raise the *idle power*.

## License

MIT
