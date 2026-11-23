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
    ASK_AFTER,
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
    EVENT_UNKNOWN_PROGRAM,
    HOLD_MARGIN,
    MAX_RUNS,
    MAX_RUNS_PER_PROGRAM,
    NAME_PROGRAM_ACTION,
    RESUME_HOURS,
    RESUME_MARGIN,
    STATUS_ACTIVE,
    STATUS_FINISHED,
    STATUS_IDLE,
    STATUS_RUNNING,
)
from .engine import (
    BIN_SECONDS,
    IDLE,
    PENDING,
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
    program_key,
    read_history_csv,
    resample,
)

_LOGGER = logging.getLogger(__name__)


STORAGE_VERSION = 1

# A finished cycle takes the program recognised while it ran when its
# duration is within this fraction of the duration shown on the display.
DISPLAY_TOLERANCE = 0.15

MESSAGES = {
    "en": {
        "finished_title": "{name}: finished",
        "finished": "{program} finished after {duration} ({energy} kWh).",
        "soon_title": "{name}: almost done",
        "soon": "{program} will finish in about {minutes} min (at {time}).",
        "program": "The cycle",
        "new_program": "Program {n}",
        "unknown_title": "{name}: which program was it?",
        "running_title": "{name}: which program is it?",
        "running": "The cycle started at {time} does not match any known program. Reply with the program name{examples}: the remaining time will follow its display duration.",
        "running_panel": "Reply from the phone notification.",
        "unknown": "A {duration} cycle did not match any known program and was saved as \u201c{program}\u201d. Reply with the program name{examples}.",
        "unknown_panel": "Reply from the phone notification, or rename \u201c{program}\u201d from the integration options (Rename a program).",
        "reply": "Reply",
    },
    "it": {
        "finished_title": "{name}: finito",
        "finished": "{program} terminato dopo {duration} ({energy} kWh).",
        "soon_title": "{name}: quasi finito",
        "soon": "{program} termina tra circa {minutes} min (alle {time}).",
        "program": "Il lavaggio",
        "new_program": "Programma {n}",
        "unknown_title": "{name}: che programma era?",
        "running_title": "{name}: che programma è?",
        "running": "Il lavaggio partito alle {time} non corrisponde a nessun programma conosciuto. Rispondi con il nome del programma{examples}: il tempo rimanente seguirà la durata del display.",
        "running_panel": "Rispondi dalla notifica sul telefono.",
        "unknown": "Un ciclo di {duration} non corrisponde a nessun programma conosciuto ed \u00e8 stato salvato come \u00ab{program}\u00bb. Rispondi con il nome del programma{examples}.",
        "unknown_panel": "Rispondi dalla notifica sul telefono, oppure rinomina \u00ab{program}\u00bb dalle opzioni dell'integrazione (Rinomina un programma).",
        "reply": "Rispondi",
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
        # program name -> duration (s) shown on the appliance display
        self.durations: dict[str, float] = {}

        # --- published state ---
        self.status = STATUS_IDLE
        self.program: str | None = None
        self.confidence = 0.0
        self.remaining: float | None = None  # seconds
        self.end_time: datetime | None = None
        self.started: datetime | None = None
        self.progress: float | None = None  # %
        self.energy: float | None = None  # Wh of the current (or last) cycle
        self.power: float | None = None  # W, last reading of the power sensor
        self._started_before_active: datetime | None = None
        self._replay_start: float | None = None
        # program with a display duration recognised in the running cycle
        self._kept_program: str | None = None
        # program the user named for the running cycle, and the cycle (start
        # timestamp) the "which program?" question was asked for
        self._user_program: str | None = None
        self._asked_cycle: float | None = None
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
        self.durations = {
            str(name): float(seconds)
            for name, seconds in (stored.get("durations") or {}).items()
        }
        self.last_cycle = stored.get("last_cycle")
        if self.last_cycle:
            self.energy = self.last_cycle.get("energy")

        # resume a cycle that was running while Home Assistant restarted
        now = dt_util.utcnow()
        since = now - timedelta(seconds=self.resume_window)
        self._replay_start = since.timestamp()
        readings = await self.async_history(since, now)
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
        self._unsubs.append(
            self.hass.bus.async_listen(
                "mobile_app_notification_action", self._async_notification_action
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
        self.power = power
        self._handle_events(self.detector.update(timestamp, power))
        self._sync_active()
        # while replaying the history the estimate is updated once per bin
        # (it sets how long the recognised program keeps the cycle open)
        if self.status == STATUS_RUNNING and self.detector.state == RUNNING and (
            self._live
            or (
                self.detector.cycle is not None
                and int((timestamp - self.detector.cycle.start) // BIN_SECONDS)
                > self._bins_done
            )
        ):
            self._update_estimate(timestamp)

    @callback
    def _sync_active(self) -> None:
        """"active" while power is drawn but no cycle is confirmed yet."""
        detector = self.detector
        if self.status == STATUS_ACTIVE and detector.state == IDLE:
            # only display or door: it never became a cycle
            self.status = STATUS_IDLE
            self.started = self._started_before_active
            return
        if detector.state != PENDING or detector.cycle is None:
            return
        if self.status == STATUS_IDLE:
            self._started_before_active = self.started
            self.status = STATUS_ACTIVE
            self.started = dt_util.utc_from_timestamp(detector.cycle.start)
            self.program = None
            self.confidence = 0.0
            self.remaining = self.end_time = self.progress = None

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
        self._kept_program = None
        self._user_program = None
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
        if self._user_program is not None:
            # named by the user: it wins over the recognition
            if program != self._user_program:
                remaining = None
            program, confidence = self._user_program, 1.0
            if remaining is None and self.display_duration(program) is None:
                # no display duration and no stored run: name only, no estimate
                self.program, self.confidence = program, confidence
                self.remaining = self.end_time = self.progress = None
                return
            remaining = remaining if remaining is not None else 0.0
        elif self.display_duration(program) is not None:
            self._kept_program = program
        elif remaining is None and self._kept_program is not None:
            # recognised earlier, but the cycle no longer looks like the stored
            # runs (e.g. a drying phase they miss): keep the program and its
            # display countdown until the end of the cycle
            remaining, program, confidence = 0.0, self._kept_program, self.confidence
        if remaining is None:
            # a program not learned yet: better no estimate than a wrong one
            self.remaining = self.end_time = self.progress = self.program = None
            self.confidence = 0.0
            if self._live and elapsed >= ASK_AFTER and self._asked_cycle != cycle.start:
                self._ask_running(cycle)
            return
        if self._asked_cycle == cycle.start and self._user_program is None:
            # recognised after the question: it is no longer needed
            self._dismiss_question(cycle.start)
        if (display := self.display_duration(program)) is not None:
            # the program is recognised: count down like the appliance display
            remaining = max(0.0, display - elapsed)
            # and do not end the cycle before the display says so
            hold = cycle.start + display - HOLD_MARGIN
            if self.detector.hold_until is None or hold > self.detector.hold_until:
                self.detector.hold_until = hold
        else:
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

    @property
    def resume_window(self) -> float:
        """Seconds of history replayed at startup."""
        longest = max(
            [*self.durations.values(), *(p.duration for p in self.profiles)], default=0.0
        )
        if longest <= 0:
            return RESUME_HOURS * 3600.0
        return longest + RESUME_MARGIN

    @callback
    def _on_finished(self, cycle: Cycle) -> None:
        if (
            not self._live
            and self._replay_start is not None
            and cycle.start <= self._replay_start + 1
        ):
            # The replayed history began while the appliance was already
            # working: only the end of that cycle is visible. It was handled
            # before the restart, so it is neither learned nor reported.
            _LOGGER.debug(
                "%s: ignoring the partial cycle at the start of the resumed history",
                self.name,
            )
            self.status = STATUS_IDLE
            self.program = None
            self.confidence = 0.0
            self.remaining = self.end_time = self.progress = None
            self._matcher = None
            return
        profile = cycle.profile()
        recognised = self.program if self.status == STATUS_RUNNING else None
        known = {p.program for p in self.profiles}
        program = self._learn(profile, program=self._user_program, hint=recognised)
        is_new = program not in known and bool(_AUTO_NAME.match(program))
        asked = self._asked_cycle == cycle.start
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
            if is_new and not asked:
                # too short to be asked while running: ask now
                self._ask_program(program, profile.duration, cycle.start)
        self._schedule_finished_timeout(cycle.end or cycle.start)

    # ------------------------------------------------------------ learning

    @callback
    def _learn(
        self, profile: Profile, program: str | None = None, hint: str | None = None
    ) -> str:
        """Store a finished run and return its program name.

        ``hint`` is the program recognised while the cycle was running: it is
        used when no stored run is close enough (e.g. only archived runs with
        a cut drying phase) but the duration matches the program's display.
        """
        for known in self.profiles:
            if abs(known.started - profile.started) < 120:
                return known.program or self._new_program_name()  # already learned
        if program is None:
            program = classify(profile, self.profiles)
        if (
            program is None
            and hint
            and (display := self.display_duration(hint))
            and abs(profile.duration - display) <= DISPLAY_TOLERANCE * display
        ):
            program = hint
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
        """Known programs with number of runs and average duration/energy.

        Archived runs are counted but left out of the averages when the
        program has newer runs. ``display`` is the duration (min) shown on the
        appliance, when known.
        """
        grouped: dict[str, list[Profile]] = {}
        for profile in self.profiles:
            grouped.setdefault(profile.program or "?", []).append(profile)
        result: dict[str, dict[str, Any]] = {}
        for name, runs in grouped.items():
            fresh = [p for p in runs if not p.archived] or runs
            info: dict[str, Any] = {
                "runs": len(runs),
                "duration": round(sum(p.duration for p in fresh) / len(fresh) / 60),
                "energy": round(sum(p.energy for p in fresh) / len(fresh) / 1000, 2),
            }
            if (display := self.display_duration(name)) is not None:
                info["display"] = round(display / 60)
            result[name] = info
        return result

    def display_duration(self, program: str | None) -> float | None:
        """Duration (s) shown on the appliance display for a program."""
        if not program:
            return None
        key = program_key(program)
        return next(
            (s for name, s in self.durations.items() if program_key(name) == key), None
        )

    def _canonical_name(self, name: str) -> str:
        """A typed name written like the known program or display entry."""
        key = program_key(name)
        for known in [*self.durations, *(p.program for p in self.profiles if p.program)]:
            if program_key(known) == key:
                return known
        return name.strip()

    async def async_set_durations(self, durations: dict[str, float]) -> None:
        """Replace the durations shown on the appliance display."""
        self.durations = {name.strip(): float(s) for name, s in durations.items()}
        await self._async_save()
        if self.status == STATUS_RUNNING and self._live:
            self._update_estimate(dt_util.utcnow().timestamp())
        self._notify_listeners()

    async def async_archive_runs(self) -> int:
        """Archive every stored run (kept for recognition only)."""
        count = 0
        for profile in self.profiles:
            if not profile.archived:
                profile.archived = True
                count += 1
        if count:
            await self._async_save()
            self._notify_listeners()
        return count

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
        new_name = self._canonical_name(new_name)
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
            self._matcher_needs_rebuild()
            await self._async_save()
            for profile in self.profiles:  # the question is answered
                if profile.program == new_name:
                    self._dismiss_question(profile.started)
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
                "durations": self.durations,
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
        self._sync_active()
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
            self._sync_active()  # a new program may already be in its pre-wash
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

    def _question_id(self, cycle_start: float) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_unknown_{int(cycle_start)}"

    @callback
    def _ask(self, cycle_start: float, title: str, message: str, panel: str, data: dict[str, Any]) -> None:
        """Ask which program a cycle is: phone (text reply), panel and event."""
        text = _messages(self.hass)
        names = list(self.durations)[:3]
        action = f"{NAME_PROGRAM_ACTION}|{self.entry.entry_id}|{int(cycle_start)}"
        for service in self.notify_services:
            domain, _, name = service.partition(".")
            if not name:
                domain, name = "notify", domain
            payload: dict[str, Any] = {"title": title, "message": message}
            if domain == "notify" and name.startswith("mobile_app"):
                payload["data"] = {
                    "tag": self._question_id(cycle_start),
                    "actions": [
                        {
                            "action": action,
                            "title": text["reply"],
                            "behavior": "textInput",
                            "textInputButtonTitle": text["reply"],
                            "textInputPlaceholder": ", ".join(names),
                        }
                    ],
                }
            elif domain == "persistent_notification":
                continue  # created below, with its own id
            self.hass.async_create_task(self._async_call_notify(domain, name, payload))
        if self.hass.services.has_service("persistent_notification", "create"):
            self.hass.async_create_task(
                self._async_call_notify(
                    "persistent_notification",
                    "create",
                    {
                        "title": title,
                        "message": f"{message}\n\n{panel}",
                        "notification_id": self._question_id(cycle_start),
                    },
                )
            )
        self.hass.bus.async_fire(
            EVENT_UNKNOWN_PROGRAM,
            {"entry_id": self.entry.entry_id, "name": self.name, "action": action, **data},
        )

    def _examples(self) -> str:
        names = list(self.durations)[:3]
        return f" ({', '.join(names)}...)" if names else ""

    @callback
    def _ask_running(self, cycle: Cycle) -> None:
        """Not recognised after ASK_AFTER: ask which program is running."""
        self._asked_cycle = cycle.start
        text = _messages(self.hass)
        started = dt_util.as_local(dt_util.utc_from_timestamp(cycle.start))
        self._ask(
            cycle.start,
            text["running_title"].format(name=self.name),
            text["running"].format(time=started.strftime("%H:%M"), examples=self._examples()),
            text["running_panel"],
            {"program": None, "started": started.isoformat(), "running": True},
        )

    @callback
    def _ask_program(self, program: str, duration: float, cycle_start: float) -> None:
        """A finished cycle of a new program: ask which one it was."""
        self._asked_cycle = cycle_start
        text = _messages(self.hass)
        self._ask(
            cycle_start,
            text["unknown_title"].format(name=self.name),
            text["unknown"].format(
                duration=_format_duration(duration), program=program, examples=self._examples()
            ),
            text["unknown_panel"].format(program=program),
            {"program": program, "duration": round(duration), "running": False},
        )

    @callback
    def _dismiss_question(self, cycle_start: float) -> None:
        if self._live and self.hass.services.has_service("persistent_notification", "dismiss"):
            self.hass.async_create_task(
                self._async_call_notify(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": self._question_id(cycle_start)},
                )
            )

    @callback
    def _async_notification_action(self, event: Event) -> None:
        """Reply to "which program is it / was it?" from the mobile app."""
        prefix, _, rest = str(event.data.get("action") or "").partition("|")
        entry_id, _, key = rest.partition("|")
        if prefix != NAME_PROGRAM_ACTION or entry_id != self.entry.entry_id or not key:
            return
        reply = str(event.data.get("reply_text") or "").strip()
        if not reply:
            return
        try:
            cycle_start = float(key)
        except ValueError:  # question sent by an older version: a program name
            self.hass.async_create_task(self.async_rename_program(key, reply))
            return
        self.hass.async_create_task(self.async_name_cycle(cycle_start, reply))

    async def async_name_cycle(self, cycle_start: float, name: str) -> bool:
        """The user said which program the cycle started at ``cycle_start`` is."""
        name = self._canonical_name(name)
        cycle = self.detector.cycle
        if self.status == STATUS_RUNNING and cycle is not None and abs(cycle.start - cycle_start) < 2:
            # still running: the program follows the user's answer
            self._user_program = name
            self._update_estimate(dt_util.utcnow().timestamp())
            self._schedule()
        elif self.last_cycle and abs(
            dt_util.parse_datetime(self.last_cycle["started"]).timestamp() - cycle_start
        ) < 120:
            await self.async_set_last_program(name)
        else:
            return False
        self._dismiss_question(cycle_start)
        self._notify_listeners()
        return True

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
