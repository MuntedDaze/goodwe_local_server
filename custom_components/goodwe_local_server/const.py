"""Integration constants and config entry defaults."""

from dataclasses import dataclass

DOMAIN = "goodwe_local_server"
TITLE = "GoodWe Local Server"
DEFAULT_UPSTREAM = "tcp.goodwe-power.com"
DEFAULT_PORT = 20001


@dataclass(frozen=True, slots=True)
class Settings:
    """Listener, cloud and meter options."""

    listen_port: int = DEFAULT_PORT
    stale_after: int = 90
    forward_to_cloud: bool = False
    upstream_host: str = DEFAULT_UPSTREAM
    upstream_port: int = DEFAULT_PORT
    upstream_address: str = ""
    solar_offset_w: int = 0
