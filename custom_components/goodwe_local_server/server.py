"""Local TCP listener, per-device state and freshness."""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .const import Settings
from .protocol import DEVICE_PREFIX, DeviceFrame, ProtocolError, decode_device, read_frame
from .relay import RelayEndpoint, RelayFailure, RelaySession, close_writer

_LOGGER = logging.getLogger(__name__)
MAX_CONNECTIONS = 32
MAX_DEVICES = 64
FRAME_TIMEOUT = 120
RELAY_QUEUE_SIZE = 8
RELAY_DRAIN_TIMEOUT = 10


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
        if self.expiry:
            self.expiry.cancel()
        self.expiry = loop.call_at(now + stale_after, self._expire)
        self._publish_telemetry()

    def cloud_result(self, result: str) -> None:
        """Record the outcome of a forwarding exchange."""
        if result == "healthy":
            self.last_cloud_success = datetime.now(UTC)
            self.counters["cloud_success"] += 1
        else:
            self.counters[result] += 1
        if result not in ("retry_backoff", "queue_full"):
            self.cloud_health = result
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
        self._clients: dict[asyncio.Task, asyncio.StreamWriter] = {}

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
            for task, writer in clients:
                writer.close()
                task.cancel()
            await asyncio.gather(*(task for task, _ in clients), return_exceptions=True)
            await server.wait_closed()
        for device in self.devices.values():
            if device.expiry:
                device.expiry.cancel()
                device.expiry = None

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.server is None or len(self._clients) >= MAX_CONNECTIONS:
            self.counters["connections_rejected"] += 1
            writer.close()
            return
        self.counters["connections"] += 1
        task = asyncio.create_task(self._handle(reader, writer, datetime.now(UTC)))
        self._clients[task] = writer
        task.add_done_callback(self._client_done)

    def _client_done(self, task: asyncio.Task) -> None:
        self._clients.pop(task)
        if not task.cancelled() and (err := task.exception()) is not None:
            self.counters["internal_errors"] += 1
            _LOGGER.error("Unexpected GoodWe connection error (%s)", type(err).__name__)

    def _device_for(self, packet: DeviceFrame, connected_at: datetime) -> DeviceState | None:
        if (device := self.devices.get(packet.serial)) is not None:
            if device.model != packet.model:
                self.counters["conflicting_devices"] += 1
                return None
            device.last_connection = max(device.last_connection, connected_at)
            device.counters["connections"] += 1
            return device
        if len(self.devices) >= MAX_DEVICES:
            self.counters["devices_rejected"] += 1
            return None
        device = DeviceState(
            packet.model,
            packet.serial,
            connected_at,
            cloud_health="waiting" if self.endpoint else "disabled",
        )
        device.counters["connections"] = 1
        self.devices[packet.serial] = device
        if self.on_device:
            self.on_device(device)
        return device

    def _record(self, name: str, device: DeviceState | None) -> None:
        self.counters[name] += 1
        if device:
            device.counters[name] += 1
            device.publish_diagnostics()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        connected_at: datetime,
    ) -> None:
        queue: asyncio.Queue[tuple[DeviceFrame, DeviceState]] = asyncio.Queue(RELAY_QUEUE_SIZE)
        relay_task: asyncio.Task | None = None
        device: DeviceState | None = None
        eof = False
        try:
            while True:
                try:
                    async with asyncio.timeout(FRAME_TIMEOUT):
                        frame = await read_frame(reader, DEVICE_PREFIX)
                except ProtocolError:
                    self._record("framing_errors", device)
                    break
                if frame is None:
                    eof = True
                    break
                if device is None and self.endpoint and frame in self.endpoint.in_flight:
                    self._record("relay_loop", device)
                    break
                try:
                    packet = decode_device(frame)
                except ProtocolError:
                    self._record("invalid_packets", device)
                    continue
                if packet is None:
                    self._record("unsupported_packets", device)
                    continue
                if device and (packet.serial, packet.model) != (device.serial, device.model):
                    self._record("identity_changes", device)
                    break
                if device is None:
                    device = self._device_for(packet, connected_at)
                    if device is None:
                        break
                self.counters["valid_packets"] += 1
                device.counters["valid_packets"] += 1
                if packet.telemetry is not None:
                    self.counters["telemetry"] += 1
                    device.receive(packet.telemetry, self.settings.stale_after)
                device.publish_diagnostics()
                if self.endpoint:
                    if queue.full():
                        self.counters["queue_full"] += 1
                        device.cloud_result("queue_full")
                        continue
                    queue.put_nowait((packet, device))
                    if relay_task is None:
                        relay_task = asyncio.create_task(self._relay(writer, queue))
        except TimeoutError:
            self._record("read_timeouts", device)
        except OSError:
            self._record("connection_errors", device)
        finally:
            try:
                if eof and relay_task is not None and not relay_task.done():
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

    async def _relay(
        self,
        writer: asyncio.StreamWriter,
        queue: asyncio.Queue[tuple[DeviceFrame, DeviceState]],
    ) -> None:
        session = RelaySession(self.endpoint, writer)
        try:
            while True:
                packet, device = await queue.get()
                if packet.raw in self.endpoint.in_flight:
                    self._record("duplicate_in_flight", device)
                    queue.task_done()
                    continue
                try:
                    self.endpoint.in_flight.add(packet.raw)
                    await session.exchange(packet)
                except RelayFailure as err:
                    reason = str(err)
                    self.counters[reason] += 1
                    device.cloud_result(reason)
                else:
                    if packet.kind != 0x10:
                        self.counters["cloud_success"] += 1
                        device.cloud_result("healthy")
                finally:
                    self.endpoint.in_flight.discard(packet.raw)
                    queue.task_done()
        finally:
            await session.close()
