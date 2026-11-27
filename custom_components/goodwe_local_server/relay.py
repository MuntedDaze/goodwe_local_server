"""Bounded SEMS relay. Local packet processing never awaits this module."""

import asyncio
import socket
from ipaddress import ip_address

from .const import Settings
from .protocol import (
    CLOUD_KEEPALIVE,
    CLOUD_PREFIX,
    DeviceFrame,
    ProtocolError,
    classify_reply,
    read_frame,
)

CONNECT_TIMEOUT = 4
REPLY_TIMEOUT = 4
CLOSE_TIMEOUT = 1
RETRY_DELAY = 10
DNS_TTL = 60
MAX_ADDRESSES = 8
MAX_REPLIES = 8


class RelayFailure(Exception):
    """A cloud forwarding failure with a privacy-safe diagnostic reason."""


def is_public_destination(address: str, local_addresses: set[str]) -> bool:
    """Reject LAN, endpoint aliases, and other non-public unicast answers."""
    ip = ip_address(address)
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return (
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and str(ip) not in local_addresses
    )


async def close_writer(writer: asyncio.StreamWriter) -> None:
    """Close either side of a TCP connection with a bounded wait."""
    writer.close()
    try:
        async with asyncio.timeout(CLOSE_TIMEOUT):
            await writer.wait_closed()
    except OSError, TimeoutError:
        writer.transport.abort()


class RelayEndpoint:
    """Resolve once per short TTL and share failed DNS lookup backoff."""

    def __init__(self, settings: Settings, local_addresses: set[str]) -> None:
        self.settings = settings
        self.local_addresses = local_addresses
        self.in_flight: set[bytes] = set()
        self._lock = asyncio.Lock()
        self._addresses: tuple[tuple[int, str], ...] = ()
        self._refresh_at = 0.0
        self._dns_error: str | None = None

    async def _resolve(self) -> tuple[tuple[int, str], ...]:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if loop.time() >= self._refresh_at:
                target = self.settings.upstream_address or self.settings.upstream_host
                self._dns_error = None
                try:
                    async with asyncio.timeout(CONNECT_TIMEOUT):
                        answers = await loop.getaddrinfo(
                            target, self.settings.upstream_port, type=socket.SOCK_STREAM
                        )
                except TimeoutError, OSError:
                    self._dns_error = "dns_failure"
                else:
                    self._addresses = tuple(
                        dict.fromkeys(
                            (family, address[0])
                            for family, _, _, _, address in answers
                            if is_public_destination(address[0], self.local_addresses)
                        )
                    )[:MAX_ADDRESSES]
                    if not self._addresses:
                        self._dns_error = "blocked_address"
                self._refresh_at = loop.time() + (RETRY_DELAY if self._dns_error else DNS_TTL)
            if self._dns_error:
                raise RelayFailure(self._dns_error)
            return self._addresses

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to a vetted numeric address so DNS cannot change between checks."""
        addresses = await self._resolve()
        for family, address in addresses:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT / len(addresses)):
                    return await asyncio.open_connection(
                        address,
                        self.settings.upstream_port,
                        family=family,
                        flags=socket.AI_NUMERICHOST,
                        limit=8192,
                    )
            except TimeoutError, OSError:
                continue
        raise RelayFailure("connect_failed")


class RelaySession:
    """One upstream socket owned by a device connection."""

    def __init__(self, endpoint: RelayEndpoint, device_writer: asyncio.StreamWriter) -> None:
        self.endpoint = endpoint
        self.device_writer = device_writer
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.retry_at = 0.0

    async def close(self) -> None:
        """Release the upstream socket."""
        if self.writer is not None:
            writer, self.writer = self.writer, None
            self.reader = None
            await close_writer(writer)

    async def exchange(self, request: DeviceFrame) -> None:
        """Send once and copy only validated replies for the current request."""
        loop = asyncio.get_running_loop()
        if loop.time() < self.retry_at:
            raise RelayFailure("retry_backoff")
        try:
            if self.writer is None or self.reader.at_eof() or self.writer.is_closing():
                await self.close()
                self.reader, self.writer = await self.endpoint.connect()
            async with asyncio.timeout(REPLY_TIMEOUT):
                self.writer.write(request.raw)
                await self.writer.drain()
                if request.kind != 0x10:
                    await self._copy_replies(request)
        except (OSError, TimeoutError, ProtocolError, RelayFailure) as err:
            self.retry_at = loop.time() + RETRY_DELAY
            await self.close()
            if isinstance(err, RelayFailure):
                raise
            reason = "reply_timeout" if isinstance(err, TimeoutError) else "upstream_disconnected"
            if isinstance(err, ProtocolError):
                reason = "invalid_reply"
            raise RelayFailure(reason) from None

    async def _copy_replies(self, request: DeviceFrame) -> None:
        for _ in range(MAX_REPLIES):
            frame = await read_frame(self.reader, CLOUD_PREFIX)
            if frame is None:
                raise RelayFailure("upstream_disconnected")
            if frame == CLOUD_KEEPALIVE:
                self.device_writer.write(frame)
                await self.device_writer.drain()
                continue
            try:
                result = classify_reply(frame, request)
            except ProtocolError:
                continue  # Frame boundary was valid. A following reply may be valid.
            if result is None:
                continue
            self.device_writer.write(frame)
            await self.device_writer.drain()
            if result == "nack":
                raise RelayFailure("cloud_rejected")
            if request.kind != 0x03 or result == "time_sync":
                return
        raise RelayFailure("invalid_reply")
