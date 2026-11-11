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

Then open **Configure** on the entry to choose:

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
activity that never reaches the start power (display, door auto-opening, a
drying fan after the end) is ignored.

**Learning.** Every finished cycle is stored as a power profile. Cycles with
the same profile are grouped into one program (`Program 1`, `Program 2`, ...).
Give them meaningful names with the `rename_program` service.

**Remaining time.** While a cycle runs, its profile so far is aligned (dynamic
time warping) with the learned runs: the best match gives the program and how
far into it the cycle is. Until a program has been learned, the remaining
time is unknown.

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

Then look at `known_programs` on the program sensor and rename them:

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
| `rename_program` | renames a learned program |
| `delete_program` | forgets a program |
| `set_last_program` | the last cycle was recognised wrongly: assign it to another (new or existing) program |

## Events and automations

`appliance_cycle_started` and `appliance_cycle_finished` are fired on the bus.
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
