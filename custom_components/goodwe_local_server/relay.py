"""Bounded SEMS relay. Local packet processing never awaits this module."""

import asyncio
import socket
from collections import Counter
from ipaddress import IPv4Address, IPv6Address, ip_address

from .const import Settings
from .protocol import (
    CLOCK_ACK_KIND,
    CLOUD_KEEPALIVE,
    CLOUD_PREFIX,
    REQUEST_KINDS,
    DeviceFrame,
    ProtocolError,
    classify_reply,
    read_frame,
)

CONNECT_TIMEOUT = 8
MIN_ADDRESS_TIMEOUT = 2
REPLY_TIMEOUT = 8
# Wait for an optional reply: a clock message after an ACK, or any answer to an
# undocumented message type.
FOLLOWUP_TIMEOUT = 3
DEVICE_WRITE_TIMEOUT = 2
CLOSE_TIMEOUT = 1
RETRY_DELAY = 10
MAX_RETRY_DELAY = 60
DNS_TTL = 60
MAX_ADDRESSES = 8
MAX_REPLIES = 8
MAX_BACKOFF_KEYS = 256
# Failures to reach SEMS at all pause forwarding for every device.
ENDPOINT_FAILURES = ("dns_failure", "blocked_address", "connect_failed")


class RelayFailure(Exception):
    """A cloud forwarding failure with a privacy-safe diagnostic reason."""

    def __init__(self, reason: str, *, blocked_by: str | None = None) -> None:
        super().__init__(reason)
        self.blocked_by = blocked_by


def _parse_address(address: str) -> IPv4Address | IPv6Address:
    """Normalize notation, IPv6 scopes and IPv4-mapped addresses."""
    ip = ip_address(address.split("%", 1)[0])
    return getattr(ip, "ipv4_mapped", None) or ip


def is_public_destination(address: str, local_addresses: set[str]) -> bool:
    """Reject LAN, endpoint aliases, and other non-public unicast answers."""
    ip = _parse_address(address)
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
    """Share DNS answers and retry backoff between all device connections.

    Devices may open a new TCP connection for every report, so neither can live
    on a single connection.
    """

    def __init__(self, settings: Settings, local_addresses: set[str]) -> None:
        self.settings = settings
        self.local_addresses = {str(_parse_address(address)) for address in local_addresses}
        self.in_flight: set[bytes] = set()
        self._lock = asyncio.Lock()
        self._addresses: tuple[tuple[int, str], ...] = ()
        self._refresh_at = 0.0
        self._dns_error: str | None = None
        # The None key covers the endpoint. Other keys are device identities.
        self._backoff: dict[bytes | None, tuple[float, int, str]] = {}

    def backoff_reason(self, identity: bytes) -> str | None:
        """Identify the active pause, including a failure on another connection."""
        now = asyncio.get_running_loop().time()
        for key in (None, identity):
            if (pause := self._backoff.get(key)) and now < pause[0]:
                return pause[2]
        return None

    def failed(self, key: bytes | None, reason: str) -> None:
        """Double the pause for each consecutive failure, up to the ceiling."""
        now = asyncio.get_running_loop().time()
        pause = self._backoff.get(key)
        # Several connections can fail together during the same endpoint outage.
        if pause and now < pause[0]:
            return
        failures = self._backoff.pop(key, (0.0, 0, ""))[1] + 1
        delay = min(RETRY_DELAY * 2 ** min(failures - 1, 16), MAX_RETRY_DELAY)
        if len(self._backoff) >= MAX_BACKOFF_KEYS:
            del self._backoff[next(other for other in self._backoff if other is not None)]
        self._backoff[key] = (now + delay, failures, reason)

    def connected(self) -> None:
        """Clear an expired endpoint pause after establishing a new socket."""
        if (pause := self._backoff.get(None)) and pause[0] <= asyncio.get_running_loop().time():
            self._backoff.pop(None)

    def succeeded(self, identity: bytes) -> None:
        """Clear the device's reply backoff after SEMS answers."""
        self._backoff.pop(identity, None)

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
        timeout = max(CONNECT_TIMEOUT / len(addresses), MIN_ADDRESS_TIMEOUT)
        for family, address in addresses:
            try:
                async with asyncio.timeout(timeout):
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
        # Counters describing the replies of the current exchange.
        self.events: Counter[str] = Counter()
        self._confirmed = False

    async def close(self) -> None:
        """Release the upstream socket."""
        if self.writer is not None:
            writer, self.writer = self.writer, None
            self.reader = None
            await close_writer(writer)

    async def exchange(self, request: DeviceFrame) -> bool:
        """Send once, copy validated replies and report whether SEMS answered."""
        self.events.clear()
        self._confirmed = False
        if blocked_by := self.endpoint.backoff_reason(request.identity):
            raise RelayFailure("retry_backoff", blocked_by=blocked_by)
        reply_required = request.kind in REQUEST_KINDS
        reused = sent = False
        try:
            if self.writer is None or self.reader.at_eof() or self.writer.is_closing():
                await self.close()
                self.reader, self.writer = await self.endpoint.connect()
                self.endpoint.connected()
            else:
                reused = True
            timeout = REPLY_TIMEOUT if reply_required else FOLLOWUP_TIMEOUT
            async with asyncio.timeout(timeout) as deadline:
                self.writer.write(request.raw)
                await self.writer.drain()
                sent = True
                if request.kind != CLOCK_ACK_KIND:
                    await self._copy_replies(request, deadline)
        except RelayFailure as err:
            reason = str(err)
        except TimeoutError:
            reason = "reply_timeout"
        except ProtocolError:
            reason = "invalid_reply"
        except OSError:
            reason = "upstream_disconnected"
        else:
            if self._confirmed:
                self.endpoint.succeeded(request.identity)
            return self._confirmed
        # The stream position is unknown after an interrupted exchange.
        await self.close()
        if reason == "cloud_rejected":
            self.endpoint.succeeded(request.identity)
        else:
            if self._confirmed:
                # An optional follow-up did not arrive.
                self.endpoint.succeeded(request.identity)
                return True
            if sent and not reply_required and reason != "invalid_reply":
                return False
            if reason in ENDPOINT_FAILURES:
                self.endpoint.failed(None, reason)
            # An idle upstream socket may simply have been closed: reconnect at once.
            elif not (reused and reason == "upstream_disconnected"):
                self.endpoint.failed(request.identity, reason)
        raise RelayFailure(reason)

    async def _copy_replies(self, request: DeviceFrame, deadline: asyncio.Timeout) -> None:
        for _ in range(MAX_REPLIES):
            frame = await read_frame(self.reader, CLOUD_PREFIX)
            if frame is None:
                raise RelayFailure("upstream_disconnected")
            if frame == CLOUD_KEEPALIVE:
                await self._deliver(frame)
                continue
            try:
                result = classify_reply(frame, request)
            except ProtocolError:
                result = None  # Frame boundary was valid. A following reply may be valid.
            if result is None:
                self.events["cloud_replies_dropped"] += 1
                continue
            await self._deliver(frame)
            if result == "nack":
                raise RelayFailure("cloud_rejected")
            if request.kind != 0x03 or result == "time_sync":
                self._confirmed = True
                return
            # SEMS may follow a time sync acknowledgement with the clock itself.
            if not self._confirmed:
                deadline.reschedule(asyncio.get_running_loop().time() + FOLLOWUP_TIMEOUT)
            self._confirmed = True
        if not self._confirmed:
            raise RelayFailure("invalid_reply")

    async def _deliver(self, frame: bytes) -> None:
        """Copy a reply to the device. A departed device is not an upstream failure."""
        if not self.device_writer.is_closing():
            try:
                async with asyncio.timeout(DEVICE_WRITE_TIMEOUT):
                    self.device_writer.write(frame)
                    await self.device_writer.drain()
                return
            except OSError, TimeoutError:
                pass
        self.events["reply_undeliverable"] += 1
