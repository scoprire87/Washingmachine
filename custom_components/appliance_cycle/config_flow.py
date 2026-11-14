"""Config flow of Appliance Cycle."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
    APPLIANCE_DEFAULTS,
    APPLIANCES,
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
    SECTION_DETECTION,
)
from .engine import has_presets, load_presets

if TYPE_CHECKING:
    from .monitor import ApplianceMonitor

STEP_RENAME = "rename_program"
STEP_LAST = "set_last_program"
STEP_IMPORT = "import_file"
STEP_DELETE = "delete_program"
STEP_PRESETS = "load_presets"
FIELD_PROGRAM = "program"
FIELD_NEW_NAME = "new_name"
FIELD_FILE = "file"


def _number(minimum: float, maximum: float, step: float, unit: str) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _notify_selector(hass: HomeAssistant) -> selector.SelectSelector:
    """Available notify services (mobile app, Telegram, persistent notification...)."""
    services = sorted(hass.services.async_services().get("notify", {}))
    options = [f"notify.{name}" for name in services]
    if "persistent_notification" in hass.services.async_services():
        options.append("persistent_notification.create")
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options, multiple=True, custom_value=True, sort=False
        )
    )


def _options_schema(hass: HomeAssistant) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_NOTIFY): _notify_selector(hass),
            vol.Required(CONF_NOTIFY_BEFORE): _number(0, 120, 1, "min"),
            vol.Required(CONF_FINISHED_TIMEOUT): _number(0, 1440, 1, "min"),
            vol.Required(SECTION_DETECTION): section(
                vol.Schema(
                    {
                        vol.Required(CONF_IDLE_POWER): _number(0, 50, 0.5, "W"),
                        vol.Required(CONF_START_POWER): _number(1, 5000, 1, "W"),
                        vol.Required(CONF_END_DELAY): _number(1, 120, 1, "min"),
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


class ApplianceCycleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add an appliance."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ApplianceCycleOptionsFlow:
        return ApplianceCycleOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_POWER_SENSOR])
            self._abort_if_unique_id_configured()
            defaults = APPLIANCE_DEFAULTS[user_input[CONF_APPLIANCE]]
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data=user_input,
                options={
                    **defaults,
                    CONF_NOTIFY: [],
                    CONF_NOTIFY_BEFORE: DEFAULT_NOTIFY_BEFORE,
                    CONF_FINISHED_TIMEOUT: DEFAULT_FINISHED_TIMEOUT,
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): selector.TextSelector(),
                vol.Required(CONF_POWER_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class=SensorDeviceClass.POWER
                    )
                ),
                vol.Required(CONF_APPLIANCE, default=APPLIANCES[0]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=APPLIANCES,
                        translation_key=CONF_APPLIANCE,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)


def _program_selector(programs: list[str], custom: bool) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=programs,
            custom_value=custom,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class ApplianceCycleOptionsFlow(OptionsFlow):
    """Settings, and management of the learned programs."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        menu = ["settings", STEP_RENAME, STEP_LAST, STEP_IMPORT, STEP_DELETE]
        appliance = self.config_entry.data.get(CONF_APPLIANCE, APPLIANCES[0])
        if await self.hass.async_add_executor_job(has_presets, appliance):
            menu.insert(4, STEP_PRESETS)
        return self.async_show_menu(step_id="init", menu_options=menu)

    # ------------------------------------------------------------ settings

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = dict(self.config_entry.options)
        appliance = self.config_entry.data.get(CONF_APPLIANCE, APPLIANCES[0])
        defaults = APPLIANCE_DEFAULTS[appliance]

        if user_input is not None:
            detection = user_input.get(SECTION_DETECTION) or {}
            return self.async_create_entry(
                data={
                    CONF_NOTIFY: list(user_input.get(CONF_NOTIFY) or []),
                    CONF_NOTIFY_BEFORE: int(user_input[CONF_NOTIFY_BEFORE]),
                    CONF_FINISHED_TIMEOUT: int(user_input[CONF_FINISHED_TIMEOUT]),
                    CONF_IDLE_POWER: float(detection.get(CONF_IDLE_POWER, defaults[CONF_IDLE_POWER])),
                    CONF_START_POWER: float(detection.get(CONF_START_POWER, defaults[CONF_START_POWER])),
                    CONF_END_DELAY: float(detection.get(CONF_END_DELAY, defaults[CONF_END_DELAY])),
                }
            )

        suggested = {
            CONF_NOTIFY: current.get(CONF_NOTIFY, []),
            CONF_NOTIFY_BEFORE: current.get(CONF_NOTIFY_BEFORE, DEFAULT_NOTIFY_BEFORE),
            CONF_FINISHED_TIMEOUT: current.get(CONF_FINISHED_TIMEOUT, DEFAULT_FINISHED_TIMEOUT),
            SECTION_DETECTION: {
                key: current.get(key, value) for key, value in defaults.items()
            },
        }
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(
                _options_schema(self.hass), suggested
            ),
        )

    # ------------------------------------------------------------ programs

    def _monitor(self) -> ApplianceMonitor | None:
        return getattr(self.config_entry, "runtime_data", None)

    def _programs(self) -> list[str]:
        monitor = self._monitor()
        return sorted(monitor.programs) if monitor is not None else []

    async def async_step_rename_program(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (monitor := self._monitor()) is None:
            return self.async_abort(reason="not_loaded")
        programs = self._programs()
        if not programs:
            return self.async_abort(reason="no_programs")
        errors: dict[str, str] = {}
        if user_input is not None:
            new_name = user_input[FIELD_NEW_NAME].strip()
            if not new_name:
                errors[FIELD_NEW_NAME] = "empty_name"
            elif await monitor.async_rename_program(user_input[FIELD_PROGRAM], new_name):
                return self.async_abort(
                    reason="program_renamed",
                    description_placeholders={
                        "program": user_input[FIELD_PROGRAM], "new_name": new_name
                    },
                )
            else:
                errors[FIELD_PROGRAM] = "unknown_program"
        schema = vol.Schema(
            {
                vol.Required(FIELD_PROGRAM): _program_selector(programs, custom=False),
                vol.Required(FIELD_NEW_NAME): selector.TextSelector(),
            }
        )
        return self.async_show_form(
            step_id=STEP_RENAME,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
            description_placeholders={"programs": _describe(monitor)},
        )

    async def async_step_set_last_program(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (monitor := self._monitor()) is None:
            return self.async_abort(reason="not_loaded")
        if not monitor.last_cycle:
            return self.async_abort(reason="no_cycle")
        if user_input is not None:
            program = user_input[FIELD_PROGRAM].strip()
            if program and await monitor.async_set_last_program(program):
                return self.async_abort(
                    reason="last_program_set", description_placeholders={"program": program}
                )
        last = monitor.last_cycle
        schema = vol.Schema(
            {
                vol.Required(
                    FIELD_PROGRAM, default=last.get("program") or ""
                ): _program_selector(self._programs(), custom=True),
            }
        )
        return self.async_show_form(
            step_id=STEP_LAST,
            data_schema=schema,
            description_placeholders={
                "program": str(last.get("program")),
                "started": str(last.get("started", ""))[:16].replace("T", " "),
                "duration": str(round((last.get("duration") or 0) / 60)),
            },
        )

    async def async_step_import_file(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (monitor := self._monitor()) is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        if user_input is not None:
            program = (user_input.get(FIELD_PROGRAM) or "").strip() or None
            try:
                text = await self.hass.async_add_executor_job(
                    _read_upload, self.hass, user_input[FIELD_FILE]
                )
                added = await monitor.async_import_text(text, program)
            except (OSError, UnicodeDecodeError, ValueError):
                errors["base"] = "invalid_file"
            else:
                if added:
                    return self.async_abort(
                        reason="file_imported",
                        description_placeholders={
                            "cycles": str(added), "programs": _describe(monitor)
                        },
                    )
                errors["base"] = "no_new_cycle"
        schema = vol.Schema(
            {
                vol.Required(FIELD_FILE): selector.FileSelector(
                    selector.FileSelectorConfig(accept=".csv,text/csv")
                ),
                vol.Optional(FIELD_PROGRAM): _program_selector(self._programs(), custom=True),
            }
        )
        return self.async_show_form(step_id=STEP_IMPORT, data_schema=schema, errors=errors)

    async def async_step_delete_program(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (monitor := self._monitor()) is None:
            return self.async_abort(reason="not_loaded")
        programs = self._programs()
        if not programs:
            return self.async_abort(reason="no_programs")
        if user_input is not None:
            deleted = [
                program
                for program in user_input[FIELD_PROGRAM]
                if await monitor.async_delete_program(program)
            ]
            if deleted:
                return self.async_abort(
                    reason="program_deleted",
                    description_placeholders={
                        "program": ", ".join(deleted), "programs": _describe(monitor)
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(FIELD_PROGRAM): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=programs,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_DELETE,
            data_schema=schema,
            description_placeholders={"programs": _describe(monitor)},
        )

    async def async_step_load_presets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (monitor := self._monitor()) is None:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            added, renamed = await monitor.async_load_presets()
            if not added and not renamed:
                return self.async_abort(reason="presets_already_loaded")
            return self.async_abort(
                reason="presets_loaded",
                description_placeholders={
                    "added": str(added), "renamed": str(renamed),
                    "programs": _describe(monitor),
                },
            )
        presets = await self.hass.async_add_executor_job(load_presets, monitor.appliance)
        names: dict[str, list[float]] = {}
        for preset in presets:
            names.setdefault(preset.program or "?", []).append(preset.duration)
        lines = "\n".join(
            f"- {name}: {round(sum(d) / len(d) / 60)} min" for name, d in names.items()
        )
        return self.async_show_form(
            step_id=STEP_PRESETS,
            data_schema=vol.Schema({}),
            description_placeholders={"presets": lines},
        )


def _describe(monitor: ApplianceMonitor) -> str:
    """Known programs, one per line (runs, average duration and energy)."""
    lines = [
        f"- {name}: {info['runs']} × {info['duration']} min, {info['energy']} kWh"
        for name, info in sorted(monitor.programs.items())
    ]
    return "\n".join(lines) or "-"


def _read_upload(hass: HomeAssistant, file_id: str) -> str:
    with process_uploaded_file(hass, file_id) as path:
        return path.read_text(encoding="utf-8-sig")
