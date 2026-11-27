"""Config and options flows for GoodWe Local Server."""

import re
from dataclasses import asdict
from ipaddress import ip_address

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlowWithReload
from homeassistant.helpers import selector

from . import entry_settings
from .const import DOMAIN, TITLE, Settings
from .relay import is_public_destination

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _normalize(settings: dict) -> dict[str, str]:
    """Normalize endpoint settings and return field-specific validation errors."""
    errors = {}
    settings["upstream_host"] = host = settings["upstream_host"].strip().lower().rstrip(".")
    settings["upstream_address"] = address = settings["upstream_address"].strip()
    if (
        not host
        or len(host) > 253
        or not all(_HOST_LABEL.fullmatch(label) for label in host.split("."))
    ):
        errors["upstream_host"] = "invalid_hostname"
    if address:
        try:
            settings["upstream_address"] = str(ip_address(address))
            if not is_public_destination(address, set()):
                errors["upstream_address"] = "non_public_address"
        except ValueError:
            errors["upstream_address"] = "invalid_address"
    return errors


def _options_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("listen_port", default=defaults["listen_port"]): vol.All(
                int, vol.Range(min=1, max=65535)
            ),
            vol.Required("stale_after", default=defaults["stale_after"]): vol.All(
                int, vol.Range(min=15, max=3600)
            ),
            vol.Required("forward_to_cloud", default=defaults["forward_to_cloud"]): bool,
            vol.Required("upstream_host", default=defaults["upstream_host"]): str,
            vol.Required("upstream_port", default=defaults["upstream_port"]): vol.All(
                int, vol.Range(min=1, max=65535)
            ),
            # A suggested value, because a default would restore a cleared override.
            vol.Optional(
                "upstream_address",
                description={"suggested_value": defaults.get("upstream_address", "")},
            ): selector.TextSelector(),
            vol.Required("solar_offset_w", default=defaults["solar_offset_w"]): vol.All(
                int, vol.Range(min=0, max=50)
            ),
        }
    )


class GoodWeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add the single shared TCP listener."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        """Confirm creation with local-only defaults."""
        self._async_abort_entries_match()
        if user_input is not None:
            return self.async_create_entry(title=TITLE, data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> GoodWeOptionsFlow:
        """Open the integration settings."""
        return GoodWeOptionsFlow()


class GoodWeOptionsFlow(OptionsFlowWithReload):
    """Validate options and reload the listener on save."""

    async def async_step_init(self, user_input: dict | None = None):
        """Show the current options or save edited values."""
        errors = {}
        defaults = asdict(entry_settings(self.config_entry))
        if user_input is not None:
            defaults = asdict(Settings()) | user_input
            errors = _normalize(defaults)
            if not errors:
                return self.async_create_entry(data=defaults)
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(defaults), errors=errors
        )
