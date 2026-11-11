"""Sensors of Appliance Cycle."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ApplianceConfigEntry
from .const import STATUS_FINISHED, STATUS_IDLE, STATUS_RUNNING
from .entity import ApplianceEntity
from .monitor import ApplianceMonitor


@dataclass(frozen=True, kw_only=True)
class ApplianceSensorDescription(SensorEntityDescription):
    """How a sensor reads the monitor."""

    value: Callable[[ApplianceMonitor], Any]
    attributes: Callable[[ApplianceMonitor], dict[str, Any]] | None = None


def _remaining(monitor: ApplianceMonitor) -> int | None:
    if monitor.status != STATUS_RUNNING or monitor.remaining is None:
        return None
    return round(monitor.remaining / 60)


SENSORS: tuple[ApplianceSensorDescription, ...] = (
    ApplianceSensorDescription(
        key="status",
        device_class=SensorDeviceClass.ENUM,
        options=[STATUS_IDLE, STATUS_RUNNING, STATUS_FINISHED],
        value=lambda m: m.status,
        attributes=lambda m: {
            "started": m.started.isoformat() if m.started else None,
            "last_cycle": m.last_cycle,
        },
    ),
    ApplianceSensorDescription(
        key="program",
        value=lambda m: m.program if m.status != STATUS_IDLE else None,
        attributes=lambda m: {
            "confidence": m.confidence if m.status == STATUS_RUNNING else None,
            "known_programs": m.programs,
        },
    ),
    ApplianceSensorDescription(
        key="remaining_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value=_remaining,
    ),
    ApplianceSensorDescription(
        key="end_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda m: m.end_time if m.status != STATUS_IDLE else None,
    ),
    ApplianceSensorDescription(
        key="progress",
        native_unit_of_measurement=PERCENTAGE,
        value=lambda m: m.progress if m.status != STATUS_IDLE else None,
    ),
    ApplianceSensorDescription(
        key="cycle_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda m: round(m.energy / 1000, 3) if m.energy is not None else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ApplianceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    monitor = entry.runtime_data
    async_add_entities(ApplianceSensor(monitor, description) for description in SENSORS)


class ApplianceSensor(ApplianceEntity, SensorEntity):
    """A value published by the monitor."""

    entity_description: ApplianceSensorDescription

    def __init__(
        self, monitor: ApplianceMonitor, description: ApplianceSensorDescription
    ) -> None:
        super().__init__(monitor, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | float | datetime | None:
        return self.entity_description.value(self.monitor)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.monitor)
