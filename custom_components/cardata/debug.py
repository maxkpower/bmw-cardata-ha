"""Dynamic debug flag handling for the Cardata integration."""

from __future__ import annotations

import logging

from .const import DEBUG_LOG

_LOGGER_NAMESPACE = "custom_components.cardata"
_DEBUG_ENABLED = DEBUG_LOG


def set_debug_enabled(value: bool) -> None:
    """Update the global debug flag.

    Only ever raises the logger level, never lowers it - see the comment below.
    """
    global _DEBUG_ENABLED
    _DEBUG_ENABLED = value
    if value:
        logging.getLogger(_LOGGER_NAMESPACE).setLevel(logging.DEBUG)
    # When disabling, leave the level alone. HA's `logger:` integration sets the
    # level on this same logger object, so writing ANY level here - including
    # NOTSET - overwrites an explicit `custom_components.cardata: debug` from
    # configuration.yaml on every setup and reload.


def debug_enabled() -> bool:
    """Return whether verbose debug logging is enabled."""
    return _DEBUG_ENABLED
