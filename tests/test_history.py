"""History import and resume after a restart (needs the recorder)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.appliance_cycle.const import DOMAIN

from .conftest import load_trace
from .test_init import POWER, PROGRAM, REMAINING, STATUS, _eco_runs, _replay, _setup


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The recorder must start before Home Assistant."""
    yield


async def test_history_import_and_resume(recorder_mock, hass, freezer):
    """Learn from the recorder history, and resume a cycle after a restart."""
    hass.states.async_set(POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    # past cycles recorded before the integration was installed
    first, second = _eco_runs()
    await _replay(hass, freezer, first)
    await _replay(hass, freezer, load_trace("quick"))
    await async_wait_recording_done(hass)
    # older than the 8 hours replayed at startup
    freezer.tick(timedelta(hours=10))

    entry = await _setup(hass)
    assert hass.states.get(PROGRAM).attributes["known_programs"] == {}
    response = await hass.services.async_call(
        DOMAIN, "import_history", {"entity_id": STATUS, "days": 2}, blocking=True, return_response=True
    )
    assert response == {"cycles": 2}
    known = hass.states.get(PROGRAM).attributes["known_programs"]
    assert sorted(known) == ["Program 1", "Program 2"]
    assert {info["runs"] for info in known.values()} == {1}
    # importing again does not duplicate the runs
    response = await hass.services.async_call(
        DOMAIN, "import_history", {"entity_id": STATUS, "days": 2}, blocking=True, return_response=True
    )
    assert response == {"cycles": 0}

    # a new eco cycle starts, then Home Assistant restarts in the middle of it
    await _replay(hass, freezer, second, until=80 * 60)
    assert hass.states.get(STATUS).state == "running"
    await async_wait_recording_done(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=60))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(STATUS)
    assert state.state == "running"
    program = hass.states.get(PROGRAM).state
    assert program == "Program 1"  # the first eco run
    remaining = float(hass.states.get(REMAINING).state)
    true_remaining = 152.4 - (80 * 60 + 60 - 120) / 60
    assert abs(remaining - true_remaining) < 8, (remaining, true_remaining)


async def test_restart_does_not_learn_the_end_of_a_past_cycle(recorder_mock, hass, freezer):
    """A restart whose replayed history starts in the middle of a finished cycle.

    The replay covers the longest known program plus 30 minutes. When a cycle
    ended an hour before the restart, the replay begins inside it: its second
    half must not be learned as a new program.
    """
    hass.states.async_set(POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    first, _ = _eco_runs()
    entry = await _setup(hass)
    await _replay(hass, freezer, first)
    await async_wait_recording_done(hass)
    known = hass.states.get(PROGRAM).attributes["known_programs"]
    assert list(known) == ["Program 1"]
    last = hass.states.get(STATUS).attributes["last_cycle"]
    monitor = entry.runtime_data
    # longest program (150.6 min) + 30 min
    assert monitor.resume_window / 60 == pytest.approx(150.6 + 30, abs=1)

    # restart one hour after the end: the replay starts ~30 min into the cycle
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(hours=1))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    known = hass.states.get(PROGRAM).attributes["known_programs"]
    assert list(known) == ["Program 1"] and known["Program 1"]["runs"] == 1
    assert hass.states.get(STATUS).attributes["last_cycle"] == last
    assert hass.states.get(STATUS).state == "idle"


async def test_resume_window_follows_the_longest_program(hass):
    entry = await _setup(hass)
    monitor = entry.runtime_data
    assert monitor.resume_window == 8 * 3600  # nothing known yet
    await monitor.async_set_durations({"Eco+": 307 * 60, "Fast": 60 * 60})
    assert monitor.resume_window == (307 + 30) * 60


def _two_quick_runs(gap_minutes: float = 10):
    """Two runs of the same short program, one right after the other."""
    run = [(t, p) for t, p in load_trace("quick")]
    end = run[-1][0]
    offset = end + gap_minutes * 60
    return run + [(t + offset, p) for t, p in run if t > 0], offset


@pytest.mark.parametrize("restart_at", ["second_running", "both_finished"])
async def test_two_short_cycles_in_a_row_and_a_restart(recorder_mock, hass, freezer, restart_at):
    """Two quick programs in a row: a restart never creates a phantom program."""
    hass.states.async_set(POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    entry = await _setup(hass, {"idle_power": 1, "end_delay": 5})
    monitor = entry.runtime_data
    await monitor.async_set_durations({"Fast": 60 * 60, "Eco+": 307 * 60})
    # the first run is learned and named
    trace, offset = _two_quick_runs()
    first = [(t, p) for t, p in trace if t < offset]
    await _replay(hass, freezer, first)
    freezer.tick(timedelta(minutes=6))
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await hass.services.async_call(
        DOMAIN, "rename_program", {"entity_id": STATUS, "program": "Program 1", "new_name": "Fast"},
        blocking=True,
    )
    # the second run starts 10 minutes after the end of the first
    second = [(t - offset + 600 - 6 * 60, p) for t, p in trace if t >= offset]
    until = 30 * 60 if restart_at == "second_running" else None
    await _replay(hass, freezer, second, until=until)
    if restart_at == "both_finished":
        freezer.tick(timedelta(minutes=10))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    runs_before = hass.states.get(PROGRAM).attributes["known_programs"]["Fast"]["runs"]
    await async_wait_recording_done(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=60))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    known = hass.states.get(PROGRAM).attributes["known_programs"]
    assert list(known) == ["Fast"], known
    assert known["Fast"]["runs"] == runs_before
    if restart_at == "second_running":
        assert hass.states.get(STATUS).state == "running"
        assert hass.states.get(PROGRAM).state == "Fast"
    else:
        assert known["Fast"]["runs"] == 2
