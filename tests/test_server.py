"""Exercise concurrent local and cloud traffic with real loopback sockets."""

import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from custom_components.goodwe_local_server import relay, server
from custom_components.goodwe_local_server.const import Settings
from custom_components.goodwe_local_server.protocol import DEVICE_PREFIX, read_frame
from custom_components.goodwe_local_server.relay import RelayEndpoint, RelayFailure

from .helpers import capture, device_packet, meter_payload, reply_packet, tcp_client, until


@asynccontextmanager
async def listening(settings=Settings(), local_addresses=None):
    runtime = server.GoodWeServer(replace(settings, listen_port=0), local_addresses or set())
    await runtime.start()
    port = runtime.server.sockets[0].getsockname()[1]
    try:
        yield runtime, port
    finally:
        await runtime.stop()


@asynccontextmanager
async def fake_sems(monkeypatch, on_connection):
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await on_connection(reader, writer)
        except OSError, asyncio.IncompleteReadError:
            pass
        finally:
            await relay.close_writer(writer)
            tasks.discard(task)

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as upstream:
        port = upstream.sockets[0].getsockname()[1]

        async def connect(_endpoint):
            return await asyncio.open_connection("127.0.0.1", port)

        monkeypatch.setattr(RelayEndpoint, "connect", connect)
        try:
            yield
        finally:
            active = tuple(tasks)
            for task in active:
                task.cancel()
            await asyncio.gather(*active)


async def test_offline_fragmentation_multiple_frames_and_concurrent_devices():
    first = device_packet(payload=meter_payload(-150))
    later = device_packet(payload=meter_payload(-400, 50))
    bad = first[:-1] + bytes([first[-1] ^ 1])
    async with listening() as (runtime, port):
        async with tcp_client(port) as (reader, writer), tcp_client(port) as (_, three_phase):
            writer.write(first[:9])
            await writer.drain()
            three_phase.write(device_packet(b"93000HKU", b"TEST0002"))
            await until(lambda: "TEST0002" in runtime.devices)
            assert "TEST0001" not in runtime.devices
            writer.write(first[9:] + bad + later)
            writer.write_eof()
            three_phase.write_eof()
            assert await asyncio.wait_for(reader.read(), 1) == b""
        device = runtime.devices["TEST0001"]
        assert device.payload == meter_payload(-400, 50)
        assert device.counters["telemetry"] == 2
        assert device.counters["invalid_packets"] == 1
        assert device.cloud_health == "disabled"
        assert runtime.devices["TEST0002"].model == "93000HKU"
        assert runtime.counters["telemetry"] == 3


async def test_backfill_and_truncation_do_not_refresh_or_generate_readings():
    async with listening(Settings(stale_after=0.07)) as (runtime, port):
        async with tcp_client(port) as (reader, writer):
            writer.write(device_packet(kind=0x45))
            device = await until(lambda: runtime.devices.get("TEST0001"))
            assert device.connection_health == "waiting"
            assert device.data_age is None
            assert device.payload is None
            writer.write(device_packet())
            await until(lambda: device.fresh)
            latest = device.last_telemetry
            writer.write(device_packet(kind=0x45) + b"POSTGW\0")
            writer.write_eof()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert device.last_telemetry == latest
            assert device.counters["framing_errors"] == 1
            await until(lambda: not device.fresh)
            assert device.connection_health == "stale"
        async with tcp_client(port) as (_reader, writer):
            writer.write(device_packet(payload=meter_payload(-402)))
            await until(lambda: device.fresh)
        assert device.counters["connections"] == 2
        assert device.connection_health == "receiving"


async def test_reporting_interval_ignores_replay_bursts_and_offline_gaps():
    async with listening(Settings(stale_after=3)) as (runtime, port):
        async with tcp_client(port) as (_reader, writer):
            writer.write(device_packet())
            device = await until(lambda: runtime.devices.get("TEST0001"))
            device.received_at -= 1.8
            writer.write(device_packet())
            assert await until(lambda: device.interval) == pytest.approx(1.8, abs=0.1)
            device.received_at -= 5
            writer.write(device_packet() + device_packet())
            await until(lambda: device.counters["telemetry"] == 4)
            assert device.interval == pytest.approx(1.8, abs=0.1)


async def test_idle_and_connection_limit_then_listener_shutdown(monkeypatch):
    monkeypatch.setattr(server, "MAX_CONNECTIONS", 1)
    monkeypatch.setattr(server, "FRAME_TIMEOUT", 0.1)
    async with listening() as (runtime, port):
        async with tcp_client(port) as (reader, _writer):
            await until(lambda: runtime.counters["connections"] == 1)
            async with tcp_client(port) as (rejected, _):
                assert await asyncio.wait_for(rejected.read(), 1) == b""
            assert runtime.counters["connections_rejected"] == 1
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert runtime.counters["read_timeouts"] == 1
        async with tcp_client(port) as (active, writer):
            writer.write(device_packet())
            device = await until(lambda: runtime.devices.get("TEST0001"))
            await runtime.stop()
            assert await asyncio.wait_for(active.read(), 1) == b""
            assert device.expiry is None
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", port)


async def test_cloud_ack_and_time_sync_passthrough_with_filtering(monkeypatch):
    metrics = capture("hk1000_capture")
    time_sync = capture("hk1000_time_sync")
    sync_replies = capture("hk1000_time_sync_replies")
    ack = capture("hk1000_ack")
    forwarded = []

    async def respond(reader, writer):
        for replies in (sync_replies, ack):
            forwarded.append(await read_frame(reader, DEVICE_PREFIX))
            writer.write(b"\x01\x02" + reply_packet(kind=0x50))
            writer.write(replies[:9])
            await writer.drain()
            await asyncio.sleep(0)
            writer.write(replies[9:])
            await writer.drain()

    async with (
        fake_sems(monkeypatch, respond),
        listening(Settings(forward_to_cloud=True)) as (runtime, port),
    ):
        async with tcp_client(port) as (reader, writer):
            writer.write(time_sync + metrics + device_packet(kind=0x99))
            writer.write_eof()
            assert await asyncio.wait_for(reader.read(), 1) == (
                b"\x01\x02" + sync_replies + b"\x01\x02" + ack
            )
        device = runtime.devices["01234567"]
        assert forwarded == [time_sync, metrics]
        assert device.cloud_health == "healthy"
        assert device.counters["cloud_success"] == 2
        assert device.counters["telemetry"] == 1
        assert device.counters["unsupported_packets"] == 1


async def test_local_telemetry_during_upstream_timeout_and_bounded_queue(monkeypatch):
    monkeypatch.setattr(relay, "REPLY_TIMEOUT", 0.12)
    monkeypatch.setattr(server, "RELAY_QUEUE_SIZE", 2)
    upstream_received = asyncio.Event()

    async def stall(reader, _writer):
        await read_frame(reader, DEVICE_PREFIX)
        upstream_received.set()
        await reader.read()

    async with (
        fake_sems(monkeypatch, stall),
        listening(Settings(forward_to_cloud=True)) as (runtime, port),
    ):
        async with tcp_client(port) as (_reader, writer):
            writer.write(device_packet())
            await asyncio.wait_for(upstream_received.wait(), 1)
            writer.write(b"".join(device_packet(payload=meter_payload(-w)) for w in range(9)))
            await until(lambda: runtime.counters["telemetry"] == 10)
            device = runtime.devices["TEST0001"]
            assert device.cloud_health == "waiting"
            assert device.payload == meter_payload(-8)
            assert device.counters["queue_full"] == 7
            await until(lambda: device.cloud_health == "reply_timeout")
            await until(lambda: device.counters["retry_backoff"] == 2)
            assert device.fresh


async def test_connection_retries_on_new_telemetry_after_upstream_disconnect(monkeypatch):
    monkeypatch.setattr(relay, "RETRY_DELAY", 0.02)
    frames = []

    async def respond(reader, writer):
        incoming = await read_frame(reader, DEVICE_PREFIX)
        if incoming:
            frames.append(incoming)
        if len(frames) == 2:
            writer.write(reply_packet())
            await writer.drain()

    async with (
        fake_sems(monkeypatch, respond),
        listening(Settings(forward_to_cloud=True)) as (runtime, port),
    ):
        async with tcp_client(port) as (reader, writer):
            first = device_packet()
            later = device_packet(payload=meter_payload(-777))
            writer.write(first)
            device = await until(lambda: runtime.devices.get("TEST0001"))
            await until(lambda: device.cloud_health == "upstream_disconnected")
            await asyncio.sleep(0.025)
            writer.write(later)
            assert await asyncio.wait_for(reader.readexactly(58), 1) == reply_packet()
            await until(lambda: device.cloud_health == "healthy")
        assert frames == [first, later]
        assert device.counters["telemetry"] == 2


@pytest.mark.parametrize("dns_error", [socket.gaierror("test failure"), TimeoutError()])
async def test_dns_failure_is_visible_but_local_is_available(monkeypatch, dns_error):
    async def fail(*_args, **_kwargs):
        raise dns_error

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fail)
    async with listening(Settings(forward_to_cloud=True)) as (runtime, port):
        async with tcp_client(port) as (_reader, writer):
            writer.write(device_packet())
            device = await until(lambda: runtime.devices.get("TEST0001"))
            await until(lambda: device.cloud_health == "dns_failure")
            assert device.fresh


async def test_dns_loop_filter_and_explicit_address_precedence(monkeypatch):
    target = []

    async def resolve(host, port, **kwargs):
        target.append(host)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in ("127.0.0.1", "192.168.4.10", "100.64.0.2", "8.8.4.4")
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    async with listening(Settings(forward_to_cloud=True), local_addresses={"8.8.4.4"}) as (
        runtime,
        port,
    ):
        async with tcp_client(port) as (_reader, writer):
            writer.write(device_packet())
            device = await until(lambda: runtime.devices.get("TEST0001"))
            await until(lambda: device.cloud_health == "blocked_address")
            assert device.fresh
    endpoint = RelayEndpoint(Settings(upstream_address="8.8.8.8"), set())

    async def connect(host, port, *, family, flags, limit):
        assert (host, port, family, flags, limit) == (
            "8.8.4.4",
            20001,
            socket.AF_INET,
            socket.AI_NUMERICHOST,
            8192,
        )
        raise OSError

    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(RelayFailure, match="connect_failed"):
        await endpoint.connect()
    assert target == ["tcp.goodwe-power.com", "8.8.8.8"]


async def test_hairpin_loop_guard(monkeypatch):
    async with listening(Settings(forward_to_cloud=True)) as (runtime, port):

        async def connect(_endpoint):
            return await asyncio.open_connection("127.0.0.1", port)

        monkeypatch.setattr(RelayEndpoint, "connect", connect)
        async with tcp_client(port) as (_reader, writer):
            writer.write(device_packet())
            await until(lambda: runtime.counters["relay_loop"] == 1)
            device = runtime.devices["TEST0001"]
            await until(lambda: device.cloud_health == "upstream_disconnected")
            assert device.counters["telemetry"] == 1
            assert runtime.counters["connections"] == 2


async def test_cloud_rejection_and_matching_invalid_reply_remain_local(monkeypatch):
    replies = (
        reply_packet(payload=b"\x02" + bytes(15)),
        b"trash unrelated to the SEMS stream",
    )

    async def respond(reader, writer):
        await read_frame(reader, DEVICE_PREFIX)
        writer.write(pending.pop(0))
        await writer.drain()

    pending = list(replies)
    async with (
        fake_sems(monkeypatch, respond),
        listening(Settings(forward_to_cloud=True)) as (runtime, port),
    ):
        async with tcp_client(port) as (reader, writer):
            writer.write(device_packet())
            assert await asyncio.wait_for(reader.readexactly(58), 1) == replies[0]
            device = await until(lambda: runtime.devices.get("TEST0001"))
            await until(lambda: device.cloud_health == "cloud_rejected")
            assert device.fresh
        async with tcp_client(port) as (reader, writer):
            writer.write(device_packet())
            await until(lambda: device.cloud_health == "invalid_reply")
            assert device.fresh
            assert device.counters["telemetry"] == 2
            writer.write_eof()
            assert await asyncio.wait_for(reader.read(), 1) == b""


async def test_device_limit_and_identity_conflicts(monkeypatch):
    monkeypatch.setattr(server, "MAX_DEVICES", 1)
    async with listening() as (runtime, port):
        async with tcp_client(port) as (reader, writer):
            writer.write(device_packet() + device_packet(serial=b"OTHERMTR"))
            assert await asyncio.wait_for(reader.read(), 1) == b""
        async with tcp_client(port) as (reader, writer):
            writer.write(device_packet(b"93000HKU"))
            assert await asyncio.wait_for(reader.read(), 1) == b""
        async with tcp_client(port) as (reader, writer):
            writer.write(device_packet(serial=b"OTHERMTR"))
            assert await asyncio.wait_for(reader.read(), 1) == b""
        device = runtime.devices["TEST0001"]
        assert device.counters["telemetry"] == 1
        assert device.counters["identity_changes"] == 1
        assert runtime.counters["conflicting_devices"] == 1
        assert runtime.counters["devices_rejected"] == 1


async def test_relay_cancels_work_on_listener_shutdown(monkeypatch):
    upstream_received = asyncio.Event()
    upstream_closed = asyncio.Event()

    async def stall(reader, _writer):
        await read_frame(reader, DEVICE_PREFIX)
        upstream_received.set()
        assert await reader.read() == b""
        upstream_closed.set()

    async with (
        fake_sems(monkeypatch, stall),
        listening(Settings(forward_to_cloud=True)) as (runtime, port),
    ):
        async with tcp_client(port) as (reader, writer):
            writer.write(device_packet())
            await asyncio.wait_for(upstream_received.wait(), 1)
            await runtime.stop()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            await asyncio.wait_for(upstream_closed.wait(), 1)
            assert not runtime.endpoint.in_flight
