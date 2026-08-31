"""Dynamic debug flag handling for the Cardata integration."""

from __future__ import annotations

import logging

from .const import DEBUG_LOG

_LOGGER_NAMESPACE = "custom_components.cardata"
_DEBUG_ENABLED = DEBUG_LOG


def set_debug_enabled(value: bool) -> None:
    """Update the global debug flag.

    Only forces the logger level when debug is explicitly switched on. Turning
    it off resets to NOTSET so that a `logger:` block in configuration.yaml
    stays authoritative instead of being overwritten on every reload.
    """
    global _DEBUG_ENABLED
    _DEBUG_ENABLED = value
    logger = logging.getLogger(_LOGGER_NAMESPACE)
    logger.setLevel(logging.DEBUG if value else logging.NOTSET)


def debug_enabled() -> bool:
    """Return whether verbose debug logging is enabled."""
    return _DEBUG_ENABLED
