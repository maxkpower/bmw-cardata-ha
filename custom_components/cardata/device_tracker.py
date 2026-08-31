"""Device tracker for BMW CarData vehicles."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

from homeassistant.components.device_tracker import TrackerEntity

try:
    from homeassistant.components.device_tracker import SourceType
except ImportError:  # Home Assistant < 2025.10
    SourceType = str  # type: ignore[assignment]
    try:
        from homeassistant.components.device_tracker.const import SOURCE_TYPE_GPS as GPS_SOURCE  # type: ignore[attr-defined]
    except ImportError:
        GPS_SOURCE = "gps"
else:
    GPS_SOURCE = SourceType.GPS

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import DOMAIN, mask_vin
from .coordinator import CardataCoordinator
from .entity import CardataEntity

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

LOCATION_DESCRIPTORS = (
    "vehicle.cabin.infotainment.navigation.currentLocation.latitude",
    "vehicle.cabin.infotainment.navigation.currentLocation.longitude",
    "vehicle.trip.segment.end.vehicleLocation.gpsPosition.latitude",
    "vehicle.trip.segment.end.vehicleLocation.gpsPosition.longitude",
)

# The pair the tracker actually publishes. LOCATION_DESCRIPTORS stays wider so
# that a trip-segment fix still creates the tracker for a VIN we have not seen.
LATITUDE_DESCRIPTOR = "vehicle.cabin.infotainment.navigation.currentLocation.latitude"
LONGITUDE_DESCRIPTOR = "vehicle.cabin.infotainment.navigation.currentLocation.longitude"
POSITION_DESCRIPTORS = (LATITUDE_DESCRIPTOR, LONGITUDE_DESCRIPTOR)

# The coordinator dispatches signal_update once per descriptor, so latitude and
# longitude are never delivered in the same signal - not even when they arrive
# in one MQTT payload. Writing the state on each signal therefore publishes a
# position built from a new latitude and the previous longitude. Coalescing the
# writes over this window collapses a fix into a single, consistent write.
POSITION_DEBOUNCE_SECONDS = 5.0

# A restored position is republished as the current one, so refuse to resurrect
# a fix old enough that the car is certainly no longer there. Set to None to
# restore regardless of age.
MAX_RESTORE_AGE_SECONDS: Optional[float] = 7 * 24 * 60 * 60


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the BMW CarData tracker from config entry."""
    runtime_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if not runtime_data:
        return

    coordinator: CardataCoordinator = runtime_data.coordinator
    trackers: Dict[str, CardataDeviceTracker] = {}

    def ensure_tracker(vin: str) -> None:
        if vin in trackers:
            return
        tracker = CardataDeviceTracker(coordinator, vin)
        trackers[vin] = tracker
        async_add_entities([tracker])
        _LOGGER.debug("Created device tracker for VIN: %s", vin)

    for vin in coordinator.data.keys():
        ensure_tracker(vin)

    async def handle_update(vin: str, descriptor: str) -> None:
        if descriptor in LOCATION_DESCRIPTORS:
            ensure_tracker(vin)

    unsub = async_dispatcher_connect(
        hass,
        coordinator.signal_update,
        handle_update,
    )
    config_entry.async_on_unload(unsub)


class CardataDeviceTracker(CardataEntity, TrackerEntity):
    """BMW CarData device tracker."""

    _attr_force_update = False
    _attr_translation_key = "car"
    _attr_name = None
    # NOT a no-op - do not delete as redundant. Home Assistant's
    # BaseTrackerEntity assigns `_attr_entity_category = EntityCategory.DIAGNOSTIC`,
    # and Entity declares the attribute as a bare annotation with no value, so
    # BaseTrackerEntity is the first *assignment* this class's MRO finds.
    # Without this line the car's location is categorised DIAGNOSTIC, which
    # banishes it to the device page's diagnostic section and drops it from the
    # auto-generated Overview dashboard entirely. Re-stating it as None here
    # overrides that inheritance, and reaches the already-registered entity on
    # restart via async_get_or_create -> _async_update_entity.
    # entity_category is purely presentational: it drives grouping and dashboard
    # inclusion. It is not an access control, and clearing it exposes nothing.
    _attr_entity_category = None

    def __init__(self, coordinator: CardataCoordinator, vin: str) -> None:
        """Initialize the tracker."""
        super().__init__(coordinator, vin, "device_tracker")
        self._attr_unique_id = f"{vin}_tracker"
        self._unsubscribe = None
        self._base_name = "Location"
        self._update_name(write_state=False)
        # Latitude and longitude are only ever replaced together, as one tuple,
        # so a half-updated pair can never be observed by the properties below.
        self._position: Optional[Tuple[float, float]] = None
        self._cancel_commit: Optional[Callable[[], None]] = None

    async def async_added_to_hass(self) -> None:
        """Handle entity added to Home Assistant."""
        await super().async_added_to_hass()
        self._restore_position(await self.async_get_last_state())
        # Prefer live data if the coordinator already holds a fix (a reload
        # rather than a cold start); this overwrites anything just restored.
        self._commit_position()
        self._unsubscribe = async_dispatcher_connect(
            self.hass,
            self._coordinator.signal_update,
            self._handle_update,
        )

    def _restore_position(self, last_state) -> None:
        """Reinstate the last published position across a restart.

        This reads back the entity state Home Assistant already persists for
        every RestoreEntity - CardataEntity is one - so it adds no new storage
        of location data. Without it the tracker comes back `unknown` after a
        restart and stays there until the next fix, because coordinates are
        deliberately not exposed as sensors and so nothing else restores them
        into the coordinator.
        """

        if last_state is None:
            return
        if MAX_RESTORE_AGE_SECONDS is not None:
            last_updated = getattr(last_state, "last_updated", None)
            if last_updated is None:
                return
            age = (dt_util.utcnow() - last_updated).total_seconds()
            if age > MAX_RESTORE_AGE_SECONDS:
                _LOGGER.debug(
                    "Discarding restored position for %s: %.0fs old",
                    mask_vin(self._vin),
                    age,
                )
                return
        try:
            lat = float(last_state.attributes["latitude"])
            lon = float(last_state.attributes["longitude"])
        except (KeyError, TypeError, ValueError):
            return
        self._position = (lat, lon)
        # Deliberately no coordinates in this message. GPS traces must not reach
        # home-assistant.log, which is what people attach to bug reports.
        _LOGGER.debug("Restored last known position for %s", mask_vin(self._vin))

    async def async_will_remove_from_hass(self) -> None:
        """Handle entity removal from Home Assistant."""
        await super().async_will_remove_from_hass()
        if self._cancel_commit is not None:
            self._cancel_commit()
            self._cancel_commit = None
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    @callback
    def _handle_update(self, vin: str, descriptor: str) -> None:
        """Handle updates from coordinator."""
        if vin != self.vin or descriptor not in POSITION_DESCRIPTORS:
            return
        # Do not write yet: the partner coordinate for this fix is dispatched in
        # a separate signal. Restart the window so a fix is written once, whole.
        if self._cancel_commit is not None:
            self._cancel_commit()
        self._cancel_commit = async_call_later(
            self.hass, POSITION_DEBOUNCE_SECONDS, self._handle_commit_due
        )

    @callback
    def _handle_commit_due(self, _now) -> None:
        """Publish the coalesced position once the window has elapsed."""
        self._cancel_commit = None
        if self._commit_position():
            self.async_write_ha_state()

    def _commit_position(self) -> bool:
        """Adopt the coordinator's latitude/longitude pair. True if it changed.

        Both values are read and stored together. If only one is available the
        pair is left untouched rather than half-applied, and nothing is ever
        derived, averaged or interpolated - a coordinate the vehicle never
        occupied is worse than one that is merely late.
        """

        lat = self._fetch_coordinate(LATITUDE_DESCRIPTOR)
        lon = self._fetch_coordinate(LONGITUDE_DESCRIPTOR)
        if lat is None or lon is None:
            return False
        position = (lat, lon)
        if position == self._position:
            return False
        self._position = position
        return True

    @property
    def source_type(self) -> SourceType | str:
        """Return the source type of the device."""
        return GPS_SOURCE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity specific state attributes."""
        # Deliberately empty. Home Assistant injects latitude/longitude into
        # this same dict, so anything constant added here is re-serialised into
        # a new recorder state_attributes row on every GPS fix - and the VIN in
        # particular must not sit next to live coordinates, where any non-admin
        # HA user can read the pair straight out of Developer Tools. Vehicle
        # metadata lives on the device registry entry.
        return {}

    def _fetch_coordinate(self, descriptor: str) -> float | None:
        state = self._coordinator.get_state(self._vin, descriptor)
        if state and state.value is not None:
            try:
                return float(state.value)
            except (ValueError, TypeError):
                # Neither the VIN nor the value itself: this is a coordinate
                # payload, and home-assistant.log is what people attach to bug
                # reports. The type alone is enough to diagnose a format change.
                _LOGGER.debug(
                    "Unable to parse coordinate for %s from descriptor %s (got %s)",
                    mask_vin(self._vin),
                    descriptor,
                    type(state.value).__name__,
                )
        return None

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        return self._position[0] if self._position else None

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        return self._position[1] if self._position else None
