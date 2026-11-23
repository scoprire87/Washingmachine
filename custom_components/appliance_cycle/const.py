"""Constants for Appliance Cycle."""
from __future__ import annotations

DOMAIN = "appliance_cycle"

CONF_NAME = "name"
CONF_POWER_SENSOR = "power_sensor"
CONF_APPLIANCE = "appliance"
CONF_IDLE_POWER = "idle_power"
CONF_START_POWER = "start_power"
CONF_END_DELAY = "end_delay"  # minutes
CONF_NOTIFY = "notify_services"
CONF_NOTIFY_BEFORE = "notify_before"  # minutes, 0 = off
CONF_FINISHED_TIMEOUT = "finished_timeout"  # minutes
SECTION_DETECTION = "detection"

APPLIANCE_DISHWASHER = "dishwasher"
APPLIANCE_WASHING_MACHINE = "washing_machine"
APPLIANCE_DRYER = "dryer"
APPLIANCE_OTHER = "other"
APPLIANCES = [
    APPLIANCE_DISHWASHER,
    APPLIANCE_WASHING_MACHINE,
    APPLIANCE_DRYER,
    APPLIANCE_OTHER,
]

# Detection defaults per appliance (idle W, start W, end delay minutes).
# Dishwasher values come from real logs: off 0 W, display 15 W, circulation
# pump 40-60 W, heater ~2 kW. After the last heating the program goes on for
# up to ~2 h (drying: fan ~15-18 W, then readings alternating 0 and 2 W every
# few seconds) and ends when the display does: the idle power stays below the
# 2 W readings so the drying belongs to the cycle (runs of 0 W inside it last
# at most ~3 min).
# Washing machines can pause for minutes while soaking, hence the longer delay.
APPLIANCE_DEFAULTS: dict[str, dict[str, float]] = {
    APPLIANCE_DISHWASHER: {CONF_IDLE_POWER: 1, CONF_START_POWER: 150, CONF_END_DELAY: 5},
    APPLIANCE_WASHING_MACHINE: {CONF_IDLE_POWER: 4, CONF_START_POWER: 100, CONF_END_DELAY: 10},
    APPLIANCE_DRYER: {CONF_IDLE_POWER: 5, CONF_START_POWER: 200, CONF_END_DELAY: 5},
    APPLIANCE_OTHER: {CONF_IDLE_POWER: 4, CONF_START_POWER: 100, CONF_END_DELAY: 5},
}

DEFAULT_NOTIFY_BEFORE = 0
DEFAULT_FINISHED_TIMEOUT = 60

# Learned runs kept per program and in total
MAX_RUNS_PER_PROGRAM = 5
MAX_RUNS = 60

# Hours of history replayed at startup to resume a cycle after a restart
# History replayed at startup to resume a cycle after a restart: the longest
# known program (display duration or learned run) plus a margin, so a cycle
# that started before Home Assistant went down is seen from its beginning;
# RESUME_HOURS when no program is known yet.
RESUME_HOURS = 8
RESUME_MARGIN = 30 * 60  # seconds
# A recognised program with a display duration is not considered finished
# before its display duration minus this margin (seconds): the drying of some
# programs (Eco) stays at 0 W for long stretches.
HOLD_MARGIN = 10 * 60
# Days of history scanned by default by the import_history service
DEFAULT_IMPORT_DAYS = 10

STATUS_IDLE = "idle"
# Drawing power, but not (yet) a confirmed cycle: pre-wash before the first
# heating, display, door. Becomes "running" when the start power is reached.
# Only from "idle": a finished appliance stays "finished" (to be unloaded)
# while its door is opened, until a new cycle is confirmed.
STATUS_ACTIVE = "active"
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"

EVENT_STARTED = f"{DOMAIN}_started"
EVENT_FINISHED = f"{DOMAIN}_finished"

SERVICE_IMPORT_HISTORY = "import_history"
SERVICE_RENAME_PROGRAM = "rename_program"
SERVICE_DELETE_PROGRAM = "delete_program"
SERVICE_SET_LAST_PROGRAM = "set_last_program"
SERVICE_IMPORT_FILE = "import_file"
SERVICE_SET_PROGRAM_DURATIONS = "set_program_durations"
SERVICE_ARCHIVE_RUNS = "archive_runs"

EVENT_UNKNOWN_PROGRAM = f"{DOMAIN}_unknown_program"
# Mobile app notification action asking which program a new cycle was:
# "<prefix>|<entry_id>|<automatic program name>"
NAME_PROGRAM_ACTION = "APPLIANCE_CYCLE_NAME_PROGRAM"

ATTR_DAYS = "days"
ATTR_PROGRAM = "program"
ATTR_NEW_NAME = "new_name"
ATTR_FILE = "file"
ATTR_PROGRAMS = "programs"
