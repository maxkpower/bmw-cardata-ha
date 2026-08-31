"""Helpers for normalising BMW CarData measurement units."""

from __future__ import annotations

from typing import Dict, Optional

# Mapping of raw unit strings returned by BMW to canonical symbols.
# Extend this as new variants appear in either streaming or API payloads.
UNIT_OVERRIDES: Dict[str, str] = {
    "percent": "%",
    # The catalogue publishes temperature as both "Celsius" and "celsius"
    # (engine coolant and tire temperatures use the first spelling, the
    # preconditioning target temperatures the second). The lookup below
    # lowercases, so one entry canonicalises both to the symbol Home Assistant
    # expects for SensorDeviceClass.TEMPERATURE.
    "celsius": "°C",
    # BMW spells litres "l"; Home Assistant's UnitOfVolume.LITERS is "L", and
    # the device class is rejected unless the unit matches exactly.
    "l": "L",
}


def normalize_unit(unit: Optional[str]) -> Optional[str]:
    """Return a canonical representation for the supplied unit string."""

    if not isinstance(unit, str):
        return unit

    stripped = unit.strip()
    if not stripped:
        return None

    mapped = UNIT_OVERRIDES.get(stripped.lower())
    if mapped is not None:
        return mapped

    return stripped

