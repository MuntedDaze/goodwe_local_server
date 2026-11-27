"""Home Assistant integration for locally pushed GoodWe telemetry."""

from dataclasses import asdict

from homeassistant.components import network
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, Settings
from .server import GoodWeServer

PLATFORMS = [Platform.SENSOR]
type GoodWeConfigEntry = ConfigEntry[GoodWeServer]


def entry_settings(entry: ConfigEntry) -> Settings:
    """Apply options over defaults, including for unchanged v0.1 entries."""
    defaults = asdict(Settings())
    return Settings(**{key: entry.options.get(key, value) for key, value in defaults.items()})


async def async_setup_entry(hass: HomeAssistant, entry: GoodWeConfigEntry) -> bool:
    """Start the listener and set up sensors."""
    settings = entry_settings(entry)
    local_addresses = set()
    if settings.forward_to_cloud:
        for adapter in await network.async_get_adapters(hass):
            local_addresses.update(address["address"] for address in adapter["ipv4"])
            local_addresses.update(address["address"] for address in adapter["ipv6"])
    runtime = GoodWeServer(settings, local_addresses)
    entry.runtime_data = runtime
    try:
        await runtime.start()
    except OSError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="listen_failed",
            translation_placeholders={"port": str(settings.listen_port)},
        ) from err

    async def stop_listener(_event) -> None:
        await runtime.stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_listener))
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await runtime.stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GoodWeConfigEntry) -> bool:
    """Unload entities and close the listener and all accepted connections."""
    runtime = entry.runtime_data
    listener, runtime.on_device = runtime.on_device, None
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        runtime.on_device = listener
        if listener:
            for device in runtime.devices.values():
                listener(device)
        return False
    await runtime.stop()
    return True
