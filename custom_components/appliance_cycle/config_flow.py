"""Config flow of Appliance Cycle."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
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


class ApplianceCycleOptionsFlow(OptionsFlow):
    """Notifications and detection thresholds."""

    async def async_step_init(
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
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _options_schema(self.hass), suggested
            ),
        )
