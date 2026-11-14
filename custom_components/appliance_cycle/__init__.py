"""Appliance Cycle: detect wash cycles from a power sensor."""
from __future__ import annotations

from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_DAYS,
    ATTR_FILE,
    ATTR_NEW_NAME,
    ATTR_PROGRAM,
    DEFAULT_IMPORT_DAYS,
    DOMAIN,
    SERVICE_DELETE_PROGRAM,
    SERVICE_IMPORT_FILE,
    SERVICE_IMPORT_HISTORY,
    SERVICE_RENAME_PROGRAM,
    SERVICE_SET_LAST_PROGRAM,
)
from .monitor import ApplianceMonitor

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type ApplianceConfigEntry = ConfigEntry[ApplianceMonitor]

_NAME = vol.All(cv.string, vol.Strip, vol.Length(min=1, max=60))
_TARGET = {vol.Required(ATTR_ENTITY_ID): cv.entity_id}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services."""

    def _monitor(call: ServiceCall) -> ApplianceMonitor:
        entity = er.async_get(hass).async_get(call.data[ATTR_ENTITY_ID])
        entry = (
            hass.config_entries.async_get_entry(entity.config_entry_id)
            if entity is not None and entity.config_entry_id
            else None
        )
        if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_appliance"
            )
        return entry.runtime_data

    async def import_history(call: ServiceCall) -> ServiceResponse:
        added = await _monitor(call).async_import_history(call.data[ATTR_DAYS])
        return {"cycles": added}

    async def import_file(call: ServiceCall) -> ServiceResponse:
        monitor = _monitor(call)
        path = Path(hass.config.path(call.data[ATTR_FILE])).resolve()
        config_dir = Path(hass.config.config_dir).resolve()
        if not (path.is_relative_to(config_dir) or hass.config.is_allowed_path(str(path))):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="file_not_allowed",
                translation_placeholders={"file": str(path)},
            )
        try:
            added = await monitor.async_import_file(str(path), call.data.get(ATTR_PROGRAM))
        except OSError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="file_not_readable",
                translation_placeholders={"file": str(path), "error": str(err)},
            ) from err
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="file_not_csv",
                translation_placeholders={"file": str(path)},
            ) from err
        return {"cycles": added}

    async def rename_program(call: ServiceCall) -> None:
        if not await _monitor(call).async_rename_program(
            call.data[ATTR_PROGRAM], call.data[ATTR_NEW_NAME]
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_program",
                translation_placeholders={"program": call.data[ATTR_PROGRAM]},
            )

    async def delete_program(call: ServiceCall) -> None:
        if not await _monitor(call).async_delete_program(call.data[ATTR_PROGRAM]):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_program",
                translation_placeholders={"program": call.data[ATTR_PROGRAM]},
            )

    async def set_last_program(call: ServiceCall) -> None:
        if not await _monitor(call).async_set_last_program(call.data[ATTR_PROGRAM]):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_cycle"
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        import_history,
        schema=vol.Schema(
            {
                **_TARGET,
                vol.Optional(ATTR_DAYS, default=DEFAULT_IMPORT_DAYS): vol.All(
                    vol.Coerce(float), vol.Range(min=0.1, max=365)
                ),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_FILE,
        import_file,
        schema=vol.Schema(
            {
                **_TARGET,
                vol.Required(ATTR_FILE): cv.string,
                vol.Optional(ATTR_PROGRAM): _NAME,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RENAME_PROGRAM,
        rename_program,
        schema=vol.Schema(
            {**_TARGET, vol.Required(ATTR_PROGRAM): _NAME, vol.Required(ATTR_NEW_NAME): _NAME}
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_PROGRAM,
        delete_program,
        schema=vol.Schema({**_TARGET, vol.Required(ATTR_PROGRAM): _NAME}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LAST_PROGRAM,
        set_last_program,
        schema=vol.Schema({**_TARGET, vol.Required(ATTR_PROGRAM): _NAME}),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ApplianceConfigEntry) -> bool:
    """Start following the appliance."""
    monitor = ApplianceMonitor(hass, entry)
    await monitor.async_start()
    entry.runtime_data = monitor
    entry.async_on_unload(monitor.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: ApplianceConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ApplianceConfigEntry) -> bool:
    """Stop following the appliance."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
