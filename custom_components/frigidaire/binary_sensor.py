from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

import frigidaire

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SOURCE_DOMAIN = "humidifier"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up frigidaire binary sensors from a config entry."""
    client = hass.data[DOMAIN][entry.entry_id]

    # One-time enumeration at setup (same call the climate/humidifier platforms
    # make). This is not recurring polling -- the entities never call the API.
    appliances = await hass.async_add_executor_job(client.get_appliances)

    entities: list[BinarySensorEntity] = []
    for appliance in appliances:
        if appliance.destination != frigidaire.Destination.DEHUMIDIFIER:
            continue
        entities.append(FrigidaireBucketFullSensor(appliance))
        entities.append(FrigidaireFilterSensor(appliance))

    async_add_entities(entities)


class FrigidaireDerivedBinarySensor(BinarySensorEntity):
    """Base class: mirrors a single attribute of the matching humidifier entity."""

    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(
        self,
        appliance,
        name_suffix: str,
        unique_suffix: str,
        source_attribute: str,
    ) -> None:
        """Initialize the derived binary sensor."""
        self._appliance: frigidaire.Appliance = appliance
        self._source_attribute = source_attribute
        self._source_entity_id: str | None = None

        # Match the humidifier entity's naming style (no device grouping), so the
        # friendly name is e.g. "Family Room Dehumidifier Bucket Full".
        self._attr_name = f"{appliance.nickname} {name_suffix}"
        self._attr_unique_id = f"{appliance.appliance_id}_{unique_suffix}"
        self._attr_is_on = None
        self._attr_available = False

    async def async_added_to_hass(self) -> None:
        """Start tracking the source humidifier entity once we are added."""
        await super().async_added_to_hass()
        self._async_resolve_source()

    @callback
    def _async_resolve_source(self) -> None:
        """Find the humidifier entity that shares our appliance id and track it.

        The humidifier platform adds its entities with update_before_add=True, so
        it typically registers *after* this binary sensor. If it isn't in the
        registry yet, wait for it to be created, then start tracking.
        """
        registry = er.async_get(self.hass)
        source_id = registry.async_get_entity_id(
            SOURCE_DOMAIN, DOMAIN, self._appliance.appliance_id
        )
        if source_id is not None:
            self._async_start_tracking(source_id)
            return

        @callback
        def _registry_listener(event: Event) -> None:
            if event.data.get("action") != "create":
                return
            resolved = registry.async_get_entity_id(
                SOURCE_DOMAIN, DOMAIN, self._appliance.appliance_id
            )
            if resolved is None:
                return
            remove_listener()
            self._async_start_tracking(resolved)

        remove_listener = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, _registry_listener
        )
        self.async_on_remove(remove_listener)

    @callback
    def _async_start_tracking(self, source_entity_id: str) -> None:
        """Seed from current state and subscribe to future source state changes."""
        self._source_entity_id = source_entity_id
        self._async_update_from_state(self.hass.states.get(source_entity_id))
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [source_entity_id], self._async_source_changed
            )
        )
        self.async_write_ha_state()

    @callback
    def _async_source_changed(self, event: Event) -> None:
        """Handle a state change on the source humidifier entity."""
        self._async_update_from_state(event.data.get("new_state"))
        self.async_write_ha_state()

    @callback
    def _async_update_from_state(self, state: State | None) -> None:
        """Update our state from the humidifier entity's published attributes."""
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._attr_available = False
            self._attr_is_on = None
            return
        self._attr_available = True
        self._attr_is_on = bool(state.attributes.get(self._source_attribute))


class FrigidaireBucketFullSensor(FrigidaireDerivedBinarySensor):
    """Binary sensor: on when the dehumidifier's water bucket is full."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, appliance) -> None:
        """Initialize the bucket-full sensor."""
        super().__init__(appliance, "Bucket Full", "bin_full", "bin_full")

    @property
    def icon(self) -> str:
        """Return a dynamic icon based on bucket state."""
        return "mdi:water-alert" if self._attr_is_on else "mdi:cup-water"


class FrigidaireFilterSensor(FrigidaireDerivedBinarySensor):
    """Binary sensor: on when the dehumidifier's filter needs cleaning."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:air-filter"

    def __init__(self, appliance) -> None:
        """Initialize the filter-status sensor."""
        super().__init__(appliance, "Filter Status", "check_filter", "check_filter")
