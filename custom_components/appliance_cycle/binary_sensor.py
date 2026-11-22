"""Binary sensor of Appliance Cycle: is a cycle running?"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ApplianceConfigEntry
from .const import STATUS_ACTIVE, STATUS_RUNNING
from .entity import ApplianceEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ApplianceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([ApplianceRunning(entry.runtime_data, "running")])


class ApplianceRunning(ApplianceEntity, BinarySensorEntity):
    """On while the appliance works: cycle running, or drawing power before it."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    @property
    def is_on(self) -> bool:
        return self.monitor.status in (STATUS_RUNNING, STATUS_ACTIVE)
