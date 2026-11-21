"""Durations from the appliance display, drying phase, archive and new programs."""
from __future__ import annotations

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_mock_service,
)

from custom_components.appliance_cycle.const import (
    APPLIANCE_DEFAULTS,
    APPLIANCE_DISHWASHER,
    CONF_END_DELAY,
    CONF_IDLE_POWER,
    CONF_START_POWER,
    DOMAIN,
    EVENT_UNKNOWN_PROGRAM,
    NAME_PROGRAM_ACTION,
)
from custom_components.appliance_cycle.engine import (
    CycleDetector,
    Profile,
    detect_cycles,
    format_duration,
    parse_duration,
    parse_durations_text,
)

from .conftest import load_trace
from .test_init import PROGRAM, REMAINING, STATUS, _advance, _replay, _setup

# The list as written on the user's dishwasher
DISPLAY = """Auto 3:19
Completo 3:46
Completo+ 4:35
Completo rapido 2:31
Completo 1/2 carico 3:11
Intenso 3:16
Intenso+ 3:50
Intenso rapido 2:43
Intenso 1/2 carico 2:42
Eco 4:39
Eco+ 5:07
Eco rapido 1:41
Eco 1/2 carico 3:45
Bicchieri 2:24
Bicchieri rapido 2:11
Bicchieri 1/2 carico 2:14
Quick shine 1:20
Fast 1:00
"""


def _dishwasher_detector() -> CycleDetector:
    defaults = APPLIANCE_DEFAULTS[APPLIANCE_DISHWASHER]
    return CycleDetector(
        idle_power=defaults[CONF_IDLE_POWER],
        start_power=defaults[CONF_START_POWER],
        end_delay=defaults[CONF_END_DELAY] * 60,
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def test_parse_durations() -> None:
    durations = parse_durations_text(DISPLAY)
    assert len(durations) == 18
    assert durations["Intenso"] == 196 * 60
    assert durations["Completo 1/2 carico"] == 191 * 60
    assert durations["Completo+"] == 275 * 60
    assert durations["Quick shine"] == 80 * 60
    assert durations["Fast"] == 60 * 60
    assert parse_durations_text("- Eco = 4:39\n\nBicchieri: 144") == {
        "Eco": 279 * 60, "Bicchieri": 144 * 60,
    }
    assert parse_duration("45 min") == 45 * 60
    assert format_duration(196 * 60) == "3:16"
    for bad in ("Intenso", "Intenso 3:75", "Intenso 0:00", "3:16"):
        with pytest.raises(ValueError):
            parse_durations_text(bad)


def test_drying_phase_belongs_to_the_cycle() -> None:
    """After the last heating the dishwasher dries for up to ~2 h at 0-2 W.

    With the old 4 W idle power the cycle ended right after the heating:
    intensive 127 min instead of 176, eco 151 instead of 268 (display 4:39).
    """
    old = CycleDetector(idle_power=4, start_power=150, end_delay=180)
    for name, cut, full in (("intensive", 127, 176), ("eco", 151, 268), ("glasses", 80, 129)):
        trace = load_trace(name)
        assert detect_cycles(trace, old)[0].duration / 60 == pytest.approx(cut, abs=1)
        cycle = detect_cycles(trace, _dishwasher_detector())[0]
        assert cycle.duration / 60 == pytest.approx(full, abs=1), name
        # the cycle ends with the last reading above 0 W, not at the timeout
        last_active = max(t for t, p in trace if p > 1 and t < cycle.end + 3600)
        assert cycle.end == pytest.approx(last_active, abs=30)


def test_display_and_door_are_still_not_cycles() -> None:
    events = []
    detector = _dishwasher_detector()
    for t, p in [(0, 0), (30, 16), (60, 15), (90, 0), (900, 35), (920, 25), (940, 0)]:
        events += detector.update(t, p)
    events += detector.tick(3000)
    assert events == []


def test_archived_flag_roundtrip() -> None:
    profile = Profile(duration=60, power=[1.0], program="Eco", archived=True)
    assert Profile.from_dict(profile.as_dict()).archived
    plain = Profile(duration=60, power=[1.0])
    assert "archived" not in plain.as_dict()  # presets / old storage unchanged
    assert not Profile.from_dict(plain.as_dict()).archived


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------

PHONE = "mobile_app_phone"
DISHWASHER = {"idle_power": 1, "start_power": 150, "end_delay": 5,
              "notify_services": [f"notify.{PHONE}"]}


def _stretched(trace, factor):
    return [(t * factor, p) for t, p in trace]


async def test_new_program_question_and_display_countdown(hass, freezer) -> None:
    phone = async_mock_service(hass, "notify", PHONE)
    created = async_mock_service(hass, "persistent_notification", "create")
    dismissed = async_mock_service(hass, "persistent_notification", "dismiss")
    unknown = async_capture_events(hass, EVENT_UNKNOWN_PROGRAM)
    await _setup(hass, DISHWASHER)

    response = await hass.services.async_call(
        DOMAIN, "set_program_durations", {"entity_id": STATUS, "programs": DISPLAY},
        blocking=True, return_response=True,
    )
    assert response == {"programs": 18}
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "set_program_durations", {"entity_id": STATUS, "programs": {"Eco": "x"}},
            blocking=True,
        )

    # first run: never seen, the whole drying phase is part of the cycle
    trace = load_trace("intensive")
    await _replay(hass, freezer, trace)
    await _advance(hass, freezer, 600)
    last = hass.states.get(STATUS).attributes["last_cycle"]
    assert last["program"] == "Program 1"
    assert last["duration"] / 60 == pytest.approx(176, abs=1)

    # ... and the user is asked which program it was
    assert [c.data["title"] for c in phone] == [
        "Dishwasher: finished", "Dishwasher: which program was it?",
    ]
    action = phone[1].data["data"]["actions"][0]
    assert action["behavior"] == "textInput"
    assert action["action"] == f"{NAME_PROGRAM_ACTION}|{hass.config_entries.async_entries(DOMAIN)[0].entry_id}|Program 1"
    assert len(created) == 1 and "Program 1" in created[0].data["message"]
    assert unknown[0].data["program"] == "Program 1"

    # replies for other appliances are ignored; the user answers "intenso"
    hass.bus.async_fire(
        "mobile_app_notification_action",
        {"action": f"{NAME_PROGRAM_ACTION}|other|Program 1", "reply_text": "Eco"},
    )
    hass.bus.async_fire(
        "mobile_app_notification_action",
        {"action": action["action"], "reply_text": "  intenso "},
    )
    await hass.async_block_till_done()
    known = hass.states.get(PROGRAM).attributes["known_programs"]
    assert list(known) == ["Intenso"]  # written like the display entry
    assert known["Intenso"]["display"] == 196
    assert dismissed and dismissed[0].data["notification_id"] == created[0].data["notification_id"]

    # second run, 12% slower: recognised, remaining time = display countdown
    second = _stretched(trace, 1.12)
    seen = {}

    def check(seconds):
        if "half" not in seen and seconds >= 100 * 60:
            seen["half"] = (
                hass.states.get(PROGRAM).state,
                float(hass.states.get(REMAINING).state),
                seconds,
            )

    await _replay(hass, freezer, second, check=check)
    program, remaining, seconds = seen["half"]
    assert program == "Intenso"
    start = next(t for t, p in second if p > 1)
    assert remaining == pytest.approx(196 - (seconds - start) / 60, abs=1.5)

    await _advance(hass, freezer, 600)
    # too long for the stored run (+12%), but it matches the display duration:
    # the program recognised while running is kept, no new question
    last = hass.states.get(STATUS).attributes["last_cycle"]
    assert last["program"] == "Intenso"
    assert last["duration"] / 60 == pytest.approx(176 * 1.12, abs=2)
    assert len([c for c in phone if "which program" in c.data["title"]]) == 1

    # archive: runs kept for recognition, left out of the averages later on
    response = await hass.services.async_call(
        DOMAIN, "archive_runs", {"entity_id": STATUS}, blocking=True, return_response=True
    )
    assert response == {"runs": 2}
    known = hass.states.get(PROGRAM).attributes["known_programs"]
    assert known["Intenso"]["runs"] == 2


async def test_programs_statistics_skip_archived_runs(hass) -> None:
    entry = await _setup(hass, DISHWASHER)
    monitor = entry.runtime_data
    monitor.profiles = [
        Profile(duration=140 * 60, power=[1.0], program="Intenso", energy=1400, archived=True),
        Profile(duration=194 * 60, power=[1.0], program="Intenso", energy=1500),
        Profile(duration=153 * 60, power=[1.0], program="Eco", energy=990, archived=True),
    ]
    await monitor.async_set_durations({"intenso": 196 * 60})
    programs = monitor.programs
    assert programs["Intenso"] == {"runs": 2, "duration": 194, "energy": 1.5, "display": 196}
    # only archived runs: they are still used
    assert programs["Eco"] == {"runs": 1, "duration": 153, "energy": 0.99}


async def test_durations_from_the_options(hass) -> None:
    entry = await _setup(hass, DISHWASHER)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "program_durations" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "program_durations"}
    )
    assert result["step_id"] == "program_durations"
    bad = await hass.config_entries.options.async_configure(
        result["flow_id"], {"durations": "Intenso tre ore"}
    )
    assert bad["errors"] == {"durations": "invalid_durations"}
    done = await hass.config_entries.options.async_configure(
        bad["flow_id"], {"durations": "Intenso 3:16\nEco 4:39"}
    )
    assert done["reason"] == "durations_saved"
    assert entry.runtime_data.durations == {"Intenso": 196 * 60, "Eco": 279 * 60}

    # the form shows the saved list again
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "program_durations"}
    )
    field = next(k for k in result["data_schema"].schema if k == "durations")
    assert field.description["suggested_value"] == "Intenso 3:16\nEco 4:39"
