"""History import and resume after a restart (needs the recorder)."""
from __future__ import annotations

from datetime import timedelta

import pytest
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
