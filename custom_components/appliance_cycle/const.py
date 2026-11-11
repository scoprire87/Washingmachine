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
# Dishwasher values come from real logs: standby 0-2 W, display 15 W, drying
# 8-9 W, circulation pump 40-60 W, heater ~2 kW, pauses inside a cycle < 30 s.
# Washing machines can pause for minutes while soaking, hence the longer delay.
APPLIANCE_DEFAULTS: dict[str, dict[str, float]] = {
    APPLIANCE_DISHWASHER: {CONF_IDLE_POWER: 4, CONF_START_POWER: 150, CONF_END_DELAY: 3},
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
RESUME_HOURS = 8
# Days of history scanned by default by the import_history service
DEFAULT_IMPORT_DAYS = 10

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"

EVENT_STARTED = f"{DOMAIN}_started"
EVENT_FINISHED = f"{DOMAIN}_finished"

SERVICE_IMPORT_HISTORY = "import_history"
SERVICE_RENAME_PROGRAM = "rename_program"
SERVICE_DELETE_PROGRAM = "delete_program"
SERVICE_SET_LAST_PROGRAM = "set_last_program"

ATTR_DAYS = "days"
ATTR_PROGRAM = "program"
ATTR_NEW_NAME = "new_name"
