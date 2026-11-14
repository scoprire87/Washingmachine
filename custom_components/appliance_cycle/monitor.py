"""Glue between Home Assistant and the cycle engine."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    APPLIANCE_DEFAULTS,
    APPLIANCE_DISHWASHER,
    CONF_APPLIANCE,
    CONF_END_DELAY,
    CONF_FINISHED_TIMEOUT,
    CONF_IDLE_POWER,
    CONF_NAME,
    CONF_NOTIFY,
    CONF_NOTIFY_BEFORE,
    CONF_POWER_SENSOR,
    CONF_START_POWER,
    DEFAULT_FINISHED_TIMEOUT,
    DEFAULT_NOTIFY_BEFORE,
    DOMAIN,
    EVENT_FINISHED,
    EVENT_STARTED,
    MAX_RUNS,
    MAX_RUNS_PER_PROGRAM,
    RESUME_HOURS,
    STATUS_FINISHED,
    STATUS_IDLE,
    STATUS_RUNNING,
)
from .engine import (
    BIN_SECONDS,
    RUNNING,
    Cycle,
    CycleDetector,
    Profile,
    ProgramMatcher,
    classify,
    detect_cycles,
    energy_wh,
    estimate,
    load_presets,
    read_history_csv,
    resample,
)

_LOGGER = logging.getLogger(__name__)


STORAGE_VERSION = 1

MESSAGES = {
    "en": {
        "finished_title": "{name}: finished",
        "finished": "{program} finished after {duration} ({energy} kWh).",
        "soon_title": "{name}: almost done",
        "soon": "{program} will finish in about {minutes} min (at {time}).",
        "program": "The cycle",
        "new_program": "Program {n}",
    },
    "it": {
        "finished_title": "{name}: finito",
        "finished": "{program} terminato dopo {duration} ({energy} kWh).",
        "soon_title": "{name}: quasi finito",
        "soon": "{program} termina tra circa {minutes} min (alle {time}).",
        "program": "Il lavaggio",
        "new_program": "Programma {n}",
    },
}

# Names given automatically to new programs, in any language
_AUTO_NAME = re.compile(
    "^(?:"
    + "|".join(re.escape(m["new_program"]).replace(r"\{n\}", r"\d+") for m in MESSAGES.values())
    + ")$"
)


def _messages(hass: HomeAssistant) -> dict[str, str]:
    language = (hass.config.language or "en").split("-")[0]
    return MESSAGES.get(language, MESSAGES["en"])


def _format_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def entry_config(entry: ConfigEntry) -> dict[str, Any]:
    """Data and options merged, with the appliance defaults."""
    config: dict[str, Any] = {**entry.data, **entry.options}
    defaults = APPLIANCE_DEFAULTS[config.get(CONF_APPLIANCE, APPLIANCE_DISHWASHER)]
    for key, value in defaults.items():
        config.setdefault(key, value)
    config.setdefault(CONF_NOTIFY, [])
    config.setdefault(CONF_NOTIFY_BEFORE, DEFAULT_NOTIFY_BEFORE)
    config.setdefault(CONF_FINISHED_TIMEOUT, DEFAULT_FINISHED_TIMEOUT)
    return config


def _power(state: State | None) -> float | None:
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if state.attributes.get("unit_of_measurement") == "kW":
        value *= 1000
    return max(value, 0.0)


class ApplianceMonitor:
    """Follows one appliance through its cycles."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        config = entry_config(entry)
        self.name: str = config[CONF_NAME]
        self.power_sensor: str = config[CONF_POWER_SENSOR]
        self.appliance: str = config.get(CONF_APPLIANCE, APPLIANCE_DISHWASHER)
        self.notify_services: list[str] = list(config[CONF_NOTIFY])
        self.notify_before = float(config[CONF_NOTIFY_BEFORE]) * 60
        self.finished_timeout = float(config[CONF_FINISHED_TIMEOUT]) * 60
        self.detector = CycleDetector(
            idle_power=float(config[CONF_IDLE_POWER]),
            start_power=float(config[CONF_START_POWER]),
            end_delay=float(config[CONF_END_DELAY]) * 60,
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.profiles: list[Profile] = []

        # --- published state ---
        self.status = STATUS_IDLE
        self.program: str | None = None
        self.confidence = 0.0
        self.remaining: float | None = None  # seconds
        self.end_time: datetime | None = None
        self.started: datetime | None = None
        self.progress: float | None = None  # %
        self.energy: float | None = None  # Wh of the current (or last) cycle
        self.last_cycle: dict[str, Any] | None = None

        self._matcher: ProgramMatcher | None = None
        self._bins_done = 0
        self._notified_soon = False
        self._live = False
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._deadline_unsub: CALLBACK_TYPE | None = None
        self._interval_unsub: CALLBACK_TYPE | None = None
        self._finished_unsub: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self.profiles = [Profile.from_dict(p) for p in stored.get("profiles", [])]
        self.last_cycle = stored.get("last_cycle")
        if self.last_cycle:
            self.energy = self.last_cycle.get("energy")

        # resume a cycle that was running while Home Assistant restarted
        now = dt_util.utcnow()
        readings = await self.async_history(now - timedelta(hours=RESUME_HOURS), now)
        for timestamp, power in readings:
            self._feed(timestamp, power)
        self._handle_events(self.detector.tick(now.timestamp()))
        if self.status == STATUS_RUNNING:
            self._update_estimate(now.timestamp())

        self._live = True
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.power_sensor], self._async_power_changed
            )
        )
        if self.detector.cycle is None and (
            power := _power(self.hass.states.get(self.power_sensor))
        ) is not None:
            self._feed(now.timestamp(), power)
        self._schedule()

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for name in ("_deadline_unsub", "_interval_unsub", "_finished_unsub"):
            if (unsub := getattr(self, name)) is not None:
                unsub()
                setattr(self, name, None)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            self._listeners.remove(listener)

        return remove

    @callback
    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    # ------------------------------------------------------------ history

    async def async_history(
        self, start: datetime, end: datetime
    ) -> list[tuple[float, float]]:
        """Readings of the power sensor from the recorder (empty without it)."""
        if "recorder" not in self.hass.config.components:
            return []
        # pylint: disable-next=import-outside-toplevel
        from homeassistant.components.recorder import get_instance, history

        def _read() -> list[State]:
            states = history.state_changes_during_period(
                self.hass,
                start,
                end,
                entity_id=self.power_sensor,
                include_start_time_state=True,
                no_attributes=False,
            )
            return states.get(self.power_sensor, [])

        try:
            states = await get_instance(self.hass).async_add_executor_job(_read)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("%s: could not read the history", self.name)
            return []
        readings = []
        for state in states:
            if (power := _power(state)) is not None:
                readings.append((state.last_changed.timestamp(), power))
        return readings

    async def async_import_history(self, days: float) -> int:
        """Learn programs from the cycles found in the recorder history."""
        now = dt_util.utcnow()
        readings = await self.async_history(now - timedelta(days=days), now)
        detector = self._new_detector()
        cycles: list[Cycle] = []
        for timestamp, power in readings:
            cycles.extend(c for kind, c in detector.update(timestamp, power) if kind == "finished")
        cycles.extend(c for kind, c in detector.tick(now.timestamp()) if kind == "finished")

        return await self._async_learn_cycles(cycles)

    async def async_import_file(self, path: str, program: str | None) -> int:
        """Learn the cycles of a CSV exported from the Home Assistant history.

        With ``program`` every cycle of the file is stored under that name.
        """

        def _read() -> str:
            with open(path, encoding="utf-8") as handle:
                return handle.read()

        text = await self.hass.async_add_executor_job(_read)
        return await self.async_import_text(text, program)

    async def async_import_text(self, text: str, program: str | None) -> int:
        """Learn the cycles of the content of a history CSV."""
        readings = read_history_csv(text)
        cycles = detect_cycles(readings, self._new_detector())
        return await self._async_learn_cycles(cycles, program)

    def _new_detector(self) -> CycleDetector:
        return CycleDetector(
            self.detector.idle_power, self.detector.start_power, self.detector.end_delay
        )

    async def _async_learn_cycles(
        self, cycles: list[Cycle], program: str | None = None
    ) -> int:
        added = 0
        for cycle in cycles:
            if any(abs(cycle.start - p.started) < 120 for p in self.profiles):
                continue
            self._learn(cycle.profile(), program)
            added += 1
        if added:
            await self._async_save()
            self._notify_listeners()
        return added

    # ------------------------------------------------------------ readings

    @callback
    def _async_power_changed(self, event: Event[EventStateChangedData]) -> None:
        power = _power(event.data["new_state"])
        if power is None:
            return
        self._feed(dt_util.utcnow().timestamp(), power)
        self._schedule()
        self._notify_listeners()

    @callback
    def _feed(self, timestamp: float, power: float) -> None:
        self._handle_events(self.detector.update(timestamp, power))
        # while replaying the history the estimate is computed once at the end
        if self._live and self.status == STATUS_RUNNING and self.detector.state == RUNNING:
            self._update_estimate(timestamp)

    @callback
    def _handle_events(self, events: list[tuple[str, Cycle]]) -> None:
        for kind, cycle in events:
            if kind == "started":
                self._on_started(cycle)
            elif kind == "finished":
                self._on_finished(cycle)

    @callback
    def _on_started(self, cycle: Cycle) -> None:
        self._cancel_finished_timer()
        self.status = STATUS_RUNNING
        self.started = dt_util.utc_from_timestamp(cycle.start)
        self.program = None
        self.confidence = 0.0
        self.remaining = None
        self.end_time = None
        self.progress = 0.0
        self.energy = 0.0
        self._matcher = ProgramMatcher(self.profiles)
        self._bins_done = 0
        self._notified_soon = False
        if self._live:
            self.hass.bus.async_fire(
                EVENT_STARTED, {"entry_id": self.entry.entry_id, "name": self.name}
            )

    @callback
    def _update_estimate(self, now: float) -> None:
        cycle = self.detector.cycle
        if cycle is None or self._matcher is None:
            return
        complete = int((now - cycle.start) // BIN_SECONDS)
        if complete > self._bins_done:
            bins = resample(cycle.samples, cycle.start, cycle.start + complete * BIN_SECONDS)
            for power in bins[self._bins_done:complete]:
                self._matcher.add_bin(power)
            self._bins_done = complete

        elapsed = now - cycle.start
        self.energy = energy_wh(cycle.samples, cycle.start, now)
        remaining, program, confidence = estimate(self._matcher.matches())
        if remaining is None:
            # a program not learned yet: better no estimate than a wrong one
            self.remaining = self.end_time = self.progress = self.program = None
            self.confidence = 0.0
            return
        # the estimate refers to the last complete bin
        remaining = max(0.0, remaining - (elapsed - self._bins_done * BIN_SECONDS))
        self.remaining = remaining
        self.program = program
        self.confidence = confidence
        self.end_time = dt_util.utc_from_timestamp(now + remaining).replace(microsecond=0)
        self.progress = round(100 * elapsed / max(elapsed + remaining, 1.0), 1)

        if (
            self._live
            and self.notify_before > 0
            and not self._notified_soon
            and remaining <= self.notify_before
            and elapsed > self.notify_before
        ):
            self._notified_soon = True
            self._send_soon_notification()

    @callback
    def _on_finished(self, cycle: Cycle) -> None:
        profile = cycle.profile()
        program = self._learn(profile)
        self.status = STATUS_FINISHED
        self.program = program
        self.confidence = 1.0
        self.remaining = 0.0
        self.end_time = dt_util.utc_from_timestamp(cycle.end or cycle.start)
        self.progress = 100.0
        self.energy = profile.energy
        self.started = dt_util.utc_from_timestamp(cycle.start)
        self.last_cycle = {
            "program": program,
            "started": self.started.isoformat(),
            "ended": self.end_time.isoformat(),
            "duration": round(profile.duration),
            "energy": round(profile.energy, 1),
        }
        self._matcher = None
        self.hass.async_create_task(self._async_save())

        if self._live:
            self.hass.bus.async_fire(
                EVENT_FINISHED,
                {"entry_id": self.entry.entry_id, "name": self.name, **self.last_cycle},
            )
            self._send_finished_notification()
        self._schedule_finished_timeout(cycle.end or cycle.start)

    # ------------------------------------------------------------ learning

    @callback
    def _learn(self, profile: Profile, program: str | None = None) -> str:
        """Store a finished run and return its program name."""
        for known in self.profiles:
            if abs(known.started - profile.started) < 120:
                return known.program or self._new_program_name()  # already learned
        if program is None:
            program = classify(profile, self.profiles)
        if program is None:
            program = self._new_program_name()
        profile.program = program
        self.profiles.append(profile)

        runs = [p for p in self.profiles if p.program == program]
        for old in runs[:-MAX_RUNS_PER_PROGRAM]:
            self.profiles.remove(old)
        if len(self.profiles) > MAX_RUNS:
            self.profiles = self.profiles[-MAX_RUNS:]
        self._matcher_needs_rebuild()
        return program

    def _new_program_name(self) -> str:
        template = _messages(self.hass)["new_program"]
        used = {p.program for p in self.profiles}
        n = 1
        while template.format(n=n) in used:
            n += 1
        return template.format(n=n)

    @callback
    def _matcher_needs_rebuild(self) -> None:
        """Profiles changed: realign the running cycle against the new set."""
        if self.status != STATUS_RUNNING or self.detector.cycle is None:
            return
        self._matcher = ProgramMatcher(self.profiles)
        self._bins_done = 0

    @property
    def programs(self) -> dict[str, dict[str, Any]]:
        """Known programs with number of runs and average duration/energy."""
        result: dict[str, dict[str, Any]] = {}
        for profile in self.profiles:
            info = result.setdefault(
                profile.program or "?", {"runs": 0, "duration": 0.0, "energy": 0.0}
            )
            info["runs"] += 1
            info["duration"] += profile.duration
            info["energy"] += profile.energy
        for info in result.values():
            info["duration"] = round(info["duration"] / info["runs"] / 60)
            info["energy"] = round(info["energy"] / info["runs"] / 1000, 2)
        return result

    async def async_load_presets(self) -> tuple[int, int]:
        """Add the bundled programs of this appliance type.

        Returns the runs added and the learned runs whose automatic name
        ("Program 2") was replaced by the matching preset program.
        """
        presets = await self.hass.async_add_executor_job(load_presets, self.appliance)
        added = 0
        for preset in presets:
            if any(abs(preset.started - p.started) < 120 for p in self.profiles):
                continue
            self.profiles.append(preset)
            added += 1
        renamed = 0
        if presets:
            for profile in self.profiles:
                if profile in presets or not _AUTO_NAME.match(profile.program or ""):
                    continue
                if (program := classify(profile, presets)) is not None:
                    if self.program == profile.program:
                        self.program = program
                    if self.last_cycle and self.last_cycle.get("started") and abs(
                        dt_util.parse_datetime(self.last_cycle["started"]).timestamp()
                        - profile.started
                    ) < 120:
                        self.last_cycle["program"] = program
                    profile.program = program
                    renamed += 1
        if added or renamed:
            self._matcher_needs_rebuild()
            await self._async_save()
            self._notify_listeners()
        return added, renamed

    async def async_rename_program(self, program: str, new_name: str) -> bool:
        found = False
        for profile in self.profiles:
            if profile.program == program:
                profile.program = new_name
                found = True
        if found:
            if self.program == program:
                self.program = new_name
            if self.last_cycle and self.last_cycle.get("program") == program:
                self.last_cycle["program"] = new_name
            await self._async_save()
            self._notify_listeners()
        return found

    async def async_delete_program(self, program: str) -> bool:
        before = len(self.profiles)
        self.profiles = [p for p in self.profiles if p.program != program]
        if len(self.profiles) == before:
            return False
        self._matcher_needs_rebuild()
        await self._async_save()
        self._notify_listeners()
        return True

    async def async_set_last_program(self, program: str) -> bool:
        """Correct the program of the last finished cycle."""
        if not self.profiles or not self.last_cycle:
            return False
        last = max(self.profiles, key=lambda p: p.started)
        last.program = program
        self.last_cycle["program"] = program
        if self.status == STATUS_FINISHED:
            self.program = program
        await self._async_save()
        self._notify_listeners()
        return True

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "profiles": [p.as_dict() for p in self.profiles],
                "last_cycle": self.last_cycle,
            }
        )

    # ------------------------------------------------------------ timers

    @callback
    def _schedule(self) -> None:
        """Timers for the end of the cycle and the periodic estimate."""
        if self._deadline_unsub is not None:
            self._deadline_unsub()
            self._deadline_unsub = None
        if (deadline := self.detector.next_deadline()) is not None:
            self._deadline_unsub = async_track_point_in_utc_time(
                self.hass,
                self._async_deadline,
                dt_util.utc_from_timestamp(deadline + 0.5),
            )

        running = self.status == STATUS_RUNNING
        if running and self._interval_unsub is None:
            self._interval_unsub = async_track_time_interval(
                self.hass, self._async_interval, timedelta(seconds=BIN_SECONDS)
            )
        elif not running and self._interval_unsub is not None:
            self._interval_unsub()
            self._interval_unsub = None

    @callback
    def _async_deadline(self, now: datetime) -> None:
        self._deadline_unsub = None
        self._handle_events(self.detector.tick(now.timestamp()))
        self._schedule()
        self._notify_listeners()

    @callback
    def _async_interval(self, now: datetime) -> None:
        if self.status == STATUS_RUNNING and self.detector.state == RUNNING:
            self._update_estimate(now.timestamp())
            self._notify_listeners()

    @callback
    def _schedule_finished_timeout(self, ended: float) -> None:
        self._cancel_finished_timer()
        if self.finished_timeout <= 0:
            return
        when = ended + self.finished_timeout
        if when <= dt_util.utcnow().timestamp():
            self.status = STATUS_IDLE
            return
        self._finished_unsub = async_track_point_in_utc_time(
            self.hass, self._async_back_to_idle, dt_util.utc_from_timestamp(when)
        )

    @callback
    def _cancel_finished_timer(self) -> None:
        if self._finished_unsub is not None:
            self._finished_unsub()
            self._finished_unsub = None

    @callback
    def _async_back_to_idle(self, _now: datetime) -> None:
        self._finished_unsub = None
        if self.status == STATUS_FINISHED:
            self.status = STATUS_IDLE
            self.remaining = None
            self.progress = None
            self._notify_listeners()

    # ------------------------------------------------------------ notifications

    @callback
    def _send(self, title: str, message: str) -> None:
        for service in self.notify_services:
            domain, _, name = service.partition(".")
            if not name:
                domain, name = "notify", domain
            self.hass.async_create_task(
                self._async_call_notify(domain, name, {"title": title, "message": message})
            )

    async def _async_call_notify(self, domain: str, name: str, data: dict[str, str]) -> None:
        try:
            await self.hass.services.async_call(domain, name, data, blocking=True)
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.warning("%s: could not send the notification via %s.%s: %s", self.name, domain, name, err)

    @callback
    def _send_finished_notification(self) -> None:
        if not self.notify_services or not self.last_cycle:
            return
        text = _messages(self.hass)
        program = self.last_cycle.get("program") or text["program"]
        self._send(
            text["finished_title"].format(name=self.name),
            text["finished"].format(
                program=program,
                duration=_format_duration(self.last_cycle["duration"]),
                energy=f"{self.last_cycle['energy'] / 1000:.2f}",
            ),
        )

    @callback
    def _send_soon_notification(self) -> None:
        if not self.notify_services or self.remaining is None or self.end_time is None:
            return
        text = _messages(self.hass)
        local_end = dt_util.as_local(self.end_time)
        self._send(
            text["soon_title"].format(name=self.name),
            text["soon"].format(
                program=self.program or text["program"],
                minutes=max(1, round(self.remaining / 60)),
                time=local_end.strftime("%H:%M"),
            ),
        )
