"""Local TCP listener, per-device state and freshness."""

import asyncio
import logging
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from .const import Settings
from .protocol import (
    DEVICE_PREFIX,
    INVERTER_MODEL,
    DeviceFrame,
    ProtocolError,
    decode_telemetry,
    parse_device,
    read_frame,
    signed,
)
from .relay import RelayEndpoint, RelayFailure, RelaySession, close_writer

_LOGGER = logging.getLogger(__name__)
MAX_CONNECTIONS = 32
MAX_CONNECTIONS_PER_PEER = 16
MAX_DEVICES = 64
FRAME_TIMEOUT = 120
# Devices send buffered reports back to back after an interruption.
RELAY_QUEUE_SIZE = 128
RELAY_DRAIN_TIMEOUT = 60
REPLAY_HISTORY = 64
CLOCK_JUMP_TOLERANCE = 120
CLOCK_MIN_SPACING = 5
# A sanity ceiling only. Device calendar fields carry no UTC offset.
MAX_CLOCK_LEAD = timedelta(days=1)
HK1000_LIFETIME_OFFSETS = (0x07, 0x0D, 0x1F)
METER_LIFETIME_OFFSETS = (*HK1000_LIFETIME_OFFSETS, 0x13, 0x19, 0x25, 0x2B, 0x31)


@dataclass(slots=True)
class DeviceState:
    """Latest accepted telemetry and counters for one device."""

    model: str
    serial: str
    last_connection: datetime
    last_telemetry: datetime | None = None
    received_at: float | None = None
    payload: bytes | None = None
    interval: float | None = None
    fresh: bool = False
    cloud_health: str = "disabled"
    last_cloud_success: datetime | None = None
    counters: Counter[str] = field(default_factory=Counter)
    telemetry_listeners: set[Callable[[], None]] = field(default_factory=set)
    diagnostic_listeners: set[Callable[[], None]] = field(default_factory=set)
    expiry: asyncio.TimerHandle | None = None
    recent: deque[bytes] = field(default_factory=lambda: deque(maxlen=REPLAY_HISTORY))
    sent_at: datetime | None = None
    clock_candidate: tuple[datetime, float, int] | None = None
    energy_high_water: dict[int, int] = field(default_factory=dict)
    regressed_energy: set[int] = field(default_factory=set)

    def screen(self, packet: DeviceFrame, stale_after: int) -> str | None:
        """Return why a reading must not replace or refresh current telemetry."""
        try:
            sent_at = datetime(2000 + packet.sent_at[0], *packet.sent_at[1:])
        except ValueError:
            return "invalid_telemetry_timestamp"
        if sent_at > datetime.now(UTC).replace(tzinfo=None) + MAX_CLOCK_LEAD:
            return "future_telemetry_timestamp"
        # The IV and other envelope bytes can change when the same report is resent.
        digest = sha256(packet.sent_at + packet.telemetry).digest()
        if digest in self.recent:
            return "duplicate_telemetry"
        if self.sent_at is not None:
            now = asyncio.get_running_loop().time()
            elapsed = now - self.received_at
            advance = (sent_at - self.sent_at).total_seconds()
            if advance <= 0 or abs(advance - elapsed) > CLOCK_JUMP_TOLERANCE:
                if not self._rebase_clock(sent_at, now, stale_after, packet.telemetry):
                    return "out_of_order_telemetry" if advance <= 0 else "telemetry_clock_jump"
        self.recent.append(digest)
        self.sent_at, self.clock_candidate = sent_at, None
        return None

    def _rebase_clock(
        self, sent_at: datetime, now: float, stale_after: int, payload: bytes
    ) -> bool:
        """Look for a new clock baseline once the old stream has gone stale."""
        offsets = (0x6F,) if self.model == INVERTER_MODEL else METER_LIFETIME_OFFSETS
        if self.fresh or any(
            signed(payload, offset, 4) < signed(self.payload, offset, 4) for offset in offsets
        ):
            self.clock_candidate = None
            return False
        count = 1
        if self.clock_candidate:
            previous_stamp, previous_receipt, previous_count = self.clock_candidate
            elapsed = now - previous_receipt
            advance = (sent_at - previous_stamp).total_seconds()
            if (
                CLOCK_MIN_SPACING <= elapsed < stale_after
                and advance > 0
                and abs(advance - elapsed) <= max(2, elapsed / 4)
            ):
                count = previous_count + 1
        self.clock_candidate = (sent_at, now, count)
        if count < 3:
            return False
        self.counters["telemetry_clock_rebased"] += 1
        return True

    @property
    def data_age(self) -> int | None:
        """Report age using the monotonic clock."""
        if self.received_at is None:
            return None
        return int(asyncio.get_running_loop().time() - self.received_at)

    @property
    def connection_health(self) -> str:
        """Report the receipt health of a periodically connecting device."""
        if self.last_telemetry is None:
            return "waiting"
        return "receiving" if self.fresh else "stale"

    def receive(self, payload: bytes, stale_after: int) -> None:
        """Replace telemetry and schedule its expiry even when traffic stops."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self.received_at is not None:
            interval = now - self.received_at
            if 1 <= interval < stale_after:
                self.interval = interval
        self.payload = payload
        self.received_at = now
        self.last_telemetry = datetime.now(UTC)
        self.fresh = True
        self.counters["telemetry"] += 1
        if self.model == "91000HKU":
            self.regressed_energy.clear()
            for offset in HK1000_LIFETIME_OFFSETS:
                value = signed(payload, offset, 4)
                if value < self.energy_high_water.get(offset, 0):
                    self.regressed_energy.add(offset)
                else:
                    self.energy_high_water[offset] = value
            if self.regressed_energy:
                self.counters["invalid_lifetime_energy"] += 1
        if self.expiry:
            self.expiry.cancel()
        self.expiry = loop.call_at(now + stale_after, self._expire)
        self._publish_telemetry()

    def cloud_result(self, result: str, *, blocked_by: str | None = None) -> None:
        """Record the outcome of a forwarding exchange."""
        if result == "healthy":
            self.last_cloud_success = datetime.now(UTC)
            self.counters["cloud_success"] += 1
        else:
            self.counters[result] += 1
        if result != "queue_full":
            self.cloud_health = blocked_by or result
        self.publish_diagnostics()

    def _expire(self) -> None:
        self.expiry = None
        self.fresh = False
        self._publish_telemetry()
        self.publish_diagnostics()

    def _publish_telemetry(self) -> None:
        for publish in self.telemetry_listeners:
            publish()

    def publish_diagnostics(self) -> None:
        """Publish changes to linked diagnostic entities."""
        for publish in self.diagnostic_listeners:
            publish()

    def diagnostics(self) -> dict:
        """Serialize diagnostics without the device serial or payload."""
        return {
            "model": self.model,
            "last_connection": self.last_connection.isoformat(),
            "last_valid_telemetry": self.last_telemetry.isoformat()
            if self.last_telemetry
            else None,
            "data_age_seconds": self.data_age,
            "reporting_interval_seconds": self.interval,
            "connection_health": self.connection_health,
            "forwarding_health": self.cloud_health,
            "last_cloud_success": (
                self.last_cloud_success.isoformat() if self.last_cloud_success else None
            ),
            "counters": dict(self.counters),
        }


class GoodWeServer:
    """Own the listener, connection tasks and device data for one config entry."""

    def __init__(self, settings: Settings, local_addresses: set[str]) -> None:
        self.settings = settings
        self.endpoint = (
            RelayEndpoint(settings, local_addresses) if settings.forward_to_cloud else None
        )
        self.devices: dict[str, DeviceState] = {}
        self.counters: Counter[str] = Counter()
        self.on_device: Callable[[DeviceState], None] | None = None
        self.server: asyncio.Server | None = None
        self._clients: dict[asyncio.Task, tuple[asyncio.StreamWriter, str]] = {}
        self._failing: set[bytes] = set()

    async def start(self) -> None:
        """Start receiving connections on the IPv4 LAN."""
        self.server = await asyncio.start_server(
            self._accept,
            "0.0.0.0",
            self.settings.listen_port,
            limit=8192,
            backlog=MAX_CONNECTIONS,
            start_serving=False,
        )
        try:
            await self.server.start_serving()
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Release the listening socket, connection tasks and freshness timers."""
        if self.server is not None:
            server, self.server = self.server, None
            server.close()
            clients = tuple(self._clients.items())
            for task, (writer, _) in clients:
                writer.close()
                task.cancel()
            await asyncio.gather(*(task for task, _ in clients), return_exceptions=True)
            await server.wait_closed()
        for device in self.devices.values():
            if device.expiry:
                device.expiry.cancel()
                device.expiry = None

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("",))[0]
        from_peer = sum(other == peer for _, other in self._clients.values())
        if (
            self.server is None
            or len(self._clients) >= MAX_CONNECTIONS
            or from_peer >= MAX_CONNECTIONS_PER_PEER
        ):
            self.counters["connections_rejected"] += 1
            writer.close()
            return
        self.counters["connections"] += 1
        task = asyncio.create_task(self._handle(reader, writer, datetime.now(UTC)))
        self._clients[task] = (writer, peer)
        task.add_done_callback(self._client_done)

    def _client_done(self, task: asyncio.Task) -> None:
        self._clients.pop(task)
        if not task.cancelled() and (err := task.exception()) is not None:
            self._internal_error(err)

    def _internal_error(self, err: BaseException) -> None:
        # The message and traceback could carry addresses or packet contents.
        self.counters["internal_errors"] += 1
        _LOGGER.error("Unexpected GoodWe connection error (%s)", type(err).__name__)

    def _device_for(self, packet: DeviceFrame, connected_at: datetime) -> DeviceState | None:
        if (device := self.devices.get(packet.serial)) is not None:
            if device.model != packet.model:
                self._record("conflicting_devices", None)
                return None
            device.last_connection = max(device.last_connection, connected_at)
            device.counters["connections"] += 1
            return device
        if len(self.devices) >= MAX_DEVICES:
            self._record("devices_rejected", None)
            return None
        device = DeviceState(
            packet.model,
            packet.serial,
            connected_at,
            cloud_health="waiting" if self.endpoint else "disabled",
        )
        device.counters["connections"] = 1
        self.devices[packet.serial] = device
        _LOGGER.info("Receiving a GoodWe %s device", packet.model)
        if self.on_device:
            self.on_device(device)
        return device

    def _known_device(self, identity: bytes) -> DeviceState | None:
        device = self.devices.get(identity[8:].decode("ascii", "replace"))
        if device and identity[:8] == device.model.encode("ascii"):
            return device
        return None

    def _record(self, name: str, device: DeviceState | None, count: int = 1) -> None:
        self.counters[name] += count
        _LOGGER.debug("GoodWe event: %s", name)
        if device:
            device.counters[name] += count
            device.publish_diagnostics()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        connected_at: datetime,
    ) -> None:
        queue: asyncio.Queue[tuple[DeviceFrame, DeviceState | None]] = asyncio.Queue(
            RELAY_QUEUE_SIZE
        )
        relay_task: asyncio.Task | None = None
        device: DeviceState | None = None
        drain = first = True
        try:
            while True:
                try:
                    async with asyncio.timeout(FRAME_TIMEOUT):
                        frame = await read_frame(reader, DEVICE_PREFIX)
                except ProtocolError:
                    self._record("framing_errors", device)
                    break
                if frame is None:
                    break
                # Buffered frames must not monopolize Home Assistant's event loop.
                await asyncio.sleep(0)
                # A forwarded frame which opens a new connection has looped back.
                if first and self.endpoint and frame in self.endpoint.in_flight:
                    self._record("relay_loop", device)
                    break
                first = False
                try:
                    message = parse_device(frame)
                except ProtocolError:
                    self._record("invalid_packets", device)
                    continue
                packet = None
                try:
                    packet = decode_telemetry(message)
                except ProtocolError:
                    # The checksum passed, so the identity can attribute the error.
                    self._record("invalid_packets", self._known_device(message.identity))
                else:
                    if packet is None:
                        self._record("unsupported_packets", self._known_device(message.identity))
                if packet is not None:
                    if device and (packet.serial, packet.model) != (device.serial, device.model):
                        self._record("identity_changes", device)
                        if not self.endpoint:
                            break
                    else:
                        if device is None:
                            device = self._device_for(packet, connected_at)
                        if device is not None:
                            self._accept_packet(packet, device)
                        elif not self.endpoint:
                            break
                # Forward every intact frame, including those not decoded locally.
                if self.endpoint:
                    target = self._known_device(message.identity)
                    if queue.full():
                        self.counters["queue_full"] += 1
                        if target:
                            target.cloud_result("queue_full")
                    else:
                        queue.put_nowait((packet or message, target))
                        if relay_task is None:
                            relay_task = asyncio.create_task(self._relay(writer, queue))
        except TimeoutError:
            self._record("read_timeouts", device)
        except OSError:
            self._record("connection_errors", device)
        except asyncio.CancelledError:
            drain = False
            raise
        finally:
            try:
                # Received frames are still forwarded unless we are shutting down.
                if drain and relay_task is not None and not relay_task.done():
                    try:
                        async with asyncio.timeout(RELAY_DRAIN_TIMEOUT):
                            await queue.join()
                    except TimeoutError:
                        self._record("relay_drain_timeout", device)
            finally:
                # Initiate closure before any further await, including on cancellation.
                writer.close()
                if relay_task is not None:
                    relay_task.cancel()
                    await asyncio.gather(relay_task, return_exceptions=True)
                await close_writer(writer)

    def _accept_packet(self, packet: DeviceFrame, device: DeviceState) -> None:
        self.counters["valid_packets"] += 1
        device.counters["valid_packets"] += 1
        if packet.telemetry is not None:
            if reason := device.screen(packet, self.settings.stale_after):
                self._record(reason, device)
            else:
                self.counters["telemetry"] += 1
                device.receive(packet.telemetry, self.settings.stale_after)
        device.publish_diagnostics()

    async def _relay(
        self,
        writer: asyncio.StreamWriter,
        queue: asyncio.Queue[tuple[DeviceFrame, DeviceState | None]],
    ) -> None:
        session = RelaySession(self.endpoint, writer)
        try:
            while True:
                packet, device = await queue.get()
                try:
                    await self._forward(session, packet, device)
                except Exception as err:  # One bad exchange must not stop forwarding.
                    self._internal_error(err)
                    self.counters["relay_error"] += 1
                    if device:
                        device.cloud_result("relay_error")
                    self._log_forwarding(packet.identity, "relay_error")
                    await session.close()
                finally:
                    queue.task_done()
        finally:
            await session.close()

    async def _forward(
        self, session: RelaySession, packet: DeviceFrame, device: DeviceState | None
    ) -> None:
        if packet.raw in self.endpoint.in_flight:
            self._record("duplicate_in_flight", device)
            return
        self.endpoint.in_flight.add(packet.raw)
        try:
            confirmed = await session.exchange(packet)
        except RelayFailure as err:
            reason = str(err)
            self.counters[reason] += 1
            if device:
                device.cloud_result(reason, blocked_by=err.blocked_by)
            self._log_forwarding(packet.identity, err.blocked_by or reason)
        else:
            if confirmed:
                self.counters["cloud_success"] += 1
                if device:
                    device.cloud_result("healthy")
                self._log_forwarding(packet.identity, None)
        finally:
            self.endpoint.in_flight.discard(packet.raw)
            for name, count in session.events.items():
                self._record(name, device, count)

    def _log_forwarding(self, identity: bytes, failure: str | None) -> None:
        """Log when a device's forwarding starts failing and when it recovers."""
        if failure == "retry_backoff":
            return
        if failure is None:
            if identity in self._failing:
                self._failing.discard(identity)
                _LOGGER.info("GoodWe SEMS forwarding recovered")
        elif identity not in self._failing and len(self._failing) < MAX_DEVICES:
            self._failing.add(identity)
            _LOGGER.warning(
                "GoodWe SEMS forwarding is failing (%s). Local telemetry is unaffected", failure
            )
