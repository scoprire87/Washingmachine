"""The "active" status: power drawn before a cycle is confirmed."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_mock_service,
)

from custom_components.appliance_cycle.const import EVENT_STARTED

from .test_init import RUNNING, STATUS, _advance, _replay, _setup

DISHWASHER = {"idle_power": 1, "start_power": 150, "end_delay": 5}


async def test_pre_wash_is_active_then_running(hass, freezer) -> None:
    async_mock_service(hass, "notify", "phone")
    started = async_capture_events(hass, EVENT_STARTED)
    await _setup(hass, DISHWASHER)
    assert hass.states.get(STATUS).state == "idle"

    # pre-wash: circulation pump only, ~48 W for 15 minutes
    await _replay(hass, freezer, [(0, 0), (60, 48), (600, 49)])
    state = hass.states.get(STATUS)
    assert state.state == "active"
    assert state.attributes["power"] == 49
    assert state.attributes["started"] is not None
    assert hass.states.get(RUNNING).state == "on"
    assert started == []  # not a confirmed cycle yet: no event, no notification

    # the heater starts: now it is a cycle
    await _replay(hass, freezer, [(900, 1850)])
    assert hass.states.get(STATUS).state == "running"
    assert len(started) == 1


async def test_display_is_active_only_while_on(hass, freezer) -> None:
    await _setup(hass, DISHWASHER)
    before = hass.states.get(STATUS).attributes["started"]
    await _replay(hass, freezer, [(0, 0), (30, 16), (60, 15)])
    assert hass.states.get(STATUS).state == "active"
    await _replay(hass, freezer, [(90, 0)])
    await _advance(hass, freezer, 600)
    state = hass.states.get(STATUS)
    assert state.state == "idle"
    assert state.attributes["started"] == before
    assert hass.states.get(RUNNING).state == "off"


async def test_finished_stays_finished_when_the_door_opens(hass, freezer) -> None:
    async_mock_service(hass, "notify", "phone")
    await _setup(hass, DISHWASHER)
    await _replay(hass, freezer, [(0, 0), (60, 1900), (1800, 1900), (1860, 0)])
    await _advance(hass, freezer, 600)
    assert hass.states.get(STATUS).state == "finished"
    # door opened to unload, the display lights up for a while
    await _replay(hass, freezer, [(0, 35), (20, 25), (300, 15), (400, 0)])
    assert hass.states.get(STATUS).state == "finished"
