"""Base entity of Appliance Cycle."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .monitor import ApplianceMonitor


class ApplianceEntity(Entity):
    """Entity updated by the appliance monitor."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, monitor: ApplianceMonitor, key: str) -> None:
        self.monitor = monitor
        self._attr_translation_key = key
        self._attr_unique_id = f"{monitor.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, monitor.entry.entry_id)},
            name=monitor.name,
            manufacturer="Appliance Cycle",
            model=monitor.appliance.replace("_", " ").capitalize(),
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.monitor.async_add_listener(self.async_write_ha_state))
