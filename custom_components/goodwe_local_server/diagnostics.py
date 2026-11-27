"""Privacy-filtered diagnostics for the config entry."""

from dataclasses import asdict

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import GoodWeConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: GoodWeConfigEntry) -> dict:
    """Export health and counters without endpoints, serial numbers, or payloads."""
    runtime = entry.runtime_data
    return {
        "settings": async_redact_data(
            asdict(runtime.settings), {"upstream_host", "upstream_address"}
        ),
        "listener_running": runtime.server is not None,
        "counters": dict(runtime.counters),
        "devices": [device.diagnostics() for device in runtime.devices.values()],
    }
