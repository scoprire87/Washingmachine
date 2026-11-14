"""Tests of the Appliance Cycle integration in Home Assistant."""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.appliance_cycle.const import (
    DOMAIN,
    EVENT_FINISHED,
    EVENT_STARTED,
)

from .conftest import load_trace

POWER = "sensor.dishwasher_power"
STATUS = "sensor.dishwasher_status"
PROGRAM = "sensor.dishwasher_program"
REMAINING = "sensor.dishwasher_remaining_time"
END = "sensor.dishwasher_estimated_end"
PROGRESS = "sensor.dishwasher_progress"
ENERGY = "sensor.dishwasher_cycle_energy"
RUNNING = "binary_sensor.dishwasher_running"


def _eco_runs():
    """The two runs of the eco program, each starting at second 0."""
    trace = load_trace("eco")
    split = next(t for t, p in trace if t > 1200 * 60 and p > 4)  # second run, next day
    first = [(t, p) for t, p in trace if t < 158 * 60]  # up to just after the end
    second = [(t - split + 120, p) for t, p in trace if split - 120 <= t < split + 158 * 60]
    return first, second


async def _setup(hass, options=None):
    hass.states.async_set(POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dishwasher",
        unique_id=POWER,
        data={"name": "Dishwasher", "power_sensor": POWER, "appliance": "dishwasher"},
        options={"idle_power": 4, "start_power": 150, "end_delay": 3,
                 "notify_services": ["notify.phone"], "notify_before": 0,
                 "finished_timeout": 60, **(options or {})},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _replay(hass, freezer, trace, until=None, check=None):
    """Set the power sensor like the real appliance did, in simulated time."""
    start = dt_util.utcnow()
    last = 0.0
    for seconds, power in trace:
        if until is not None and seconds > until:
            break
        # let the periodic timers run while the power does not change
        while seconds - last > 30:
            last += 30
            freezer.move_to(start + timedelta(seconds=last))
            async_fire_time_changed(hass, dt_util.utcnow())
        freezer.move_to(start + timedelta(seconds=seconds))
        last = seconds
        hass.states.async_set(POWER, str(power), {"unit_of_measurement": "W"})
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        if check:
            check(seconds)
    return start


async def _advance(hass, freezer, seconds):
    for _ in range(int(seconds // 30)):
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()


async def test_config_flow(hass):
    hass.states.async_set(POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Dishwasher", "power_sensor": POWER, "appliance": "washing_machine"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = result["result"]
    # defaults of the appliance type
    assert entry.options["end_delay"] == 10 and entry.options["start_power"] == 100
    await hass.async_block_till_done()
    assert hass.states.get(STATUS).state == "idle"

    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    again = await hass.config_entries.flow.async_configure(
        again["flow_id"], {"name": "Other", "power_sensor": POWER, "appliance": "dishwasher"}
    )
    assert again["type"] == FlowResultType.ABORT

    options = await hass.config_entries.options.async_init(entry.entry_id)
    assert options["type"] == FlowResultType.FORM
    options = await hass.config_entries.options.async_configure(
        options["flow_id"],
        {"notify_services": ["notify.phone"], "notify_before": 10, "finished_timeout": 30,
         "detection": {"idle_power": 5, "start_power": 200, "end_delay": 4}},
    )
    assert options["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["notify_before"] == 10 and entry.options["end_delay"] == 4.0


async def test_learns_and_recognises_programs(hass, freezer):
    notify = async_mock_service(hass, "notify", "phone")
    started = async_capture_events(hass, EVENT_STARTED)
    finished = async_capture_events(hass, EVENT_FINISHED)
    entry = await _setup(hass)
    first, second = _eco_runs()

    # first run: nothing known yet, the cycle is detected and learned
    await _replay(hass, freezer, first)
    await _advance(hass, freezer, 240)
    assert hass.states.get(STATUS).state == "finished"
    assert hass.states.get(RUNNING).state == "off"
    assert len(started) == 1 and len(finished) == 1
    assert finished[0].data["program"] == "Program 1"
    assert finished[0].data["duration"] / 60 == pytest.approx(150.6, abs=0.5)
    assert float(hass.states.get(ENERGY).state) == pytest.approx(0.87, abs=0.02)
    assert len(notify) == 1
    assert notify[0].data["title"] == "Dishwasher: finished"
    assert "Program 1 finished after 2 h 31 min" in notify[0].data["message"]

    # the user names the program
    await hass.services.async_call(
        DOMAIN, "rename_program", {"entity_id": STATUS, "program": "Program 1", "new_name": "Eco"}, blocking=True
    )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "rename_program", {"entity_id": STATUS, "program": "Nope", "new_name": "X"}, blocking=True
        )

    # second run: recognised, with a remaining time and an estimated end
    total = 152.4 * 60
    checks = {}

    def check(seconds):
        elapsed = seconds - 120  # the trace starts 2 minutes before the cycle
        for fraction in (0.5, 0.9):
            if fraction not in checks and elapsed >= total * fraction:
                checks[fraction] = (
                    hass.states.get(PROGRAM).state,
                    float(hass.states.get(REMAINING).state),
                    (total - elapsed) / 60,
                    hass.states.get(STATUS).state,
                    hass.states.get(RUNNING).state,
                    hass.states.get(END).state,
                )

    await _replay(hass, freezer, second, check=check)
    for fraction, (program, remaining, true_remaining, status, running, end) in checks.items():
        assert program == "Eco", fraction
        assert status == "running" and running == "on"
        assert end not in ("unknown", "unavailable")
        assert abs(remaining - true_remaining) < 6, (fraction, remaining, true_remaining)

    await _advance(hass, freezer, 240)
    assert hass.states.get(STATUS).state == "finished"
    assert hass.states.get(PROGRAM).state == "Eco"
    assert len(notify) == 2 and "Eco finished" in notify[1].data["message"]
    known = hass.states.get(PROGRAM).attributes["known_programs"]
    assert known["Eco"]["runs"] == 2

    # after the "finished" time the status goes back to idle
    await _advance(hass, freezer, 3600)
    assert hass.states.get(STATUS).state == "idle"
    assert hass.states.get(REMAINING).state == "unknown"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_reminder_before_the_end(hass, freezer):
    notify = async_mock_service(hass, "notify", "phone")
    await _setup(hass, {"notify_before": 10})
    first, second = _eco_runs()
    await _replay(hass, freezer, first)
    await _advance(hass, freezer, 240)
    assert len(notify) == 1  # no reminder without a known program

    sent_at = []

    def check(seconds):
        if len(notify) == 2 and not sent_at:
            sent_at.append((seconds - 120) / 60)

    await _replay(hass, freezer, second, check=check)
    assert notify[1].data["title"] == "Dishwasher: almost done"
    # about 10 minutes before the real end (152.4 min)
    assert 152.4 - sent_at[0] == pytest.approx(10, abs=4)
    await _advance(hass, freezer, 240)
    assert len(notify) == 3 and notify[2].data["title"] == "Dishwasher: finished"


async def test_display_and_door_are_not_cycles(hass, freezer):
    notify = async_mock_service(hass, "notify", "phone")
    events = async_capture_events(hass, EVENT_STARTED)
    await _setup(hass)
    # display switched on, then the door opening after the end of a program
    await _replay(hass, freezer, [(0, 0), (30, 16), (60, 15), (90, 0), (900, 35), (920, 25), (940, 0)])
    await _advance(hass, freezer, 300)
    assert hass.states.get(STATUS).state == "idle"
    assert events == [] and notify == []


async def test_set_last_and_delete_program(hass, freezer):
    await _setup(hass)
    trace = load_trace("quick")
    await _replay(hass, freezer, trace)
    await _advance(hass, freezer, 240)
    assert hass.states.get(PROGRAM).state == "Program 1"
    await hass.services.async_call(
        DOMAIN, "set_last_program", {"entity_id": STATUS, "program": "Quick"}, blocking=True
    )
    assert hass.states.get(PROGRAM).state == "Quick"
    assert hass.states.get(STATUS).attributes["last_cycle"]["program"] == "Quick"
    await hass.services.async_call(
        DOMAIN, "delete_program", {"entity_id": STATUS, "program": "Quick"}, blocking=True
    )
    assert hass.states.get(PROGRAM).attributes["known_programs"] == {}
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "delete_program", {"entity_id": STATUS, "program": "Quick"}, blocking=True
        )



async def test_import_file_and_unknown_program(hass, freezer, tmp_path):
    hass.config.config_dir = str(tmp_path)
    async_mock_service(hass, "notify", "phone")
    await _setup(hass)

    # only Eco is known: a quick program gets no (wrong) estimate
    first, _ = _eco_runs()
    await _replay(hass, freezer, first)
    await _advance(hass, freezer, 240)
    trace = load_trace("eco_quick_2")
    await _replay(hass, freezer, trace, until=20 * 60)
    assert hass.states.get(STATUS).state == "running"
    assert hass.states.get(REMAINING).state == "unknown"
    assert hass.states.get(PROGRAM).state == "unknown"
    assert hass.states.get(END).state == "unknown"

    # a log exported from the history of Home Assistant
    start = dt_util.parse_datetime("2026-10-01T10:00:00+00:00")
    lines = ["entity_id,state,last_changed"] + [
        f"sensor.plug_power,{power},{(start + timedelta(seconds=s)).isoformat()}"
        for s, power in load_trace("eco_quick")
    ]
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "eco_quick.csv").write_text("\n".join(lines) + "\n")
    data = {"entity_id": STATUS, "file": "logs/eco_quick.csv", "program": "Eco quick"}
    response = await hass.services.async_call(
        DOMAIN, "import_file", data, blocking=True, return_response=True
    )
    assert response == {"cycles": 1}
    assert await hass.services.async_call(
        DOMAIN, "import_file", data, blocking=True, return_response=True
    ) == {"cycles": 0}

    # the running cycle is realigned with the new program
    await _advance(hass, freezer, 60)
    assert hass.states.get(PROGRAM).state == "Eco quick"
    # the trace starts 3 minutes before the cycle: 18 of 69.5 minutes elapsed
    remaining = float(hass.states.get(REMAINING).state)
    assert abs(remaining - (69.5 - 18)) < 2

    for bad, error in (("../outside.csv", "file_not_allowed"), ("logs/missing.csv", "file_not_readable")):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                DOMAIN, "import_file", {"entity_id": STATUS, "file": bad}, blocking=True
            )
        assert err.value.translation_key == error
