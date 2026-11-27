"""Regression tests for defects found by the independent v0.2 audit."""

import asyncio
import logging
import os
import socket
import struct

import pytest

from custom_components.goodwe_local_server import relay, server
from custom_components.goodwe_local_server.const import Settings
from custom_components.goodwe_local_server.protocol import (
    DEVICE_PREFIX,
    classify_reply,
    decode_device,
    read_frame,
)
from custom_components.goodwe_local_server.relay import RelayEndpoint, RelayFailure
from custom_components.goodwe_local_server.sensor import HK1000_READINGS, METER_REGISTERS

from .helpers import (
    capture,
    captured_body,
    device_packet,
    meter_payload,
    reply_packet,
    stamp,
    tcp_client,
    time_set_packet,
    until,
)
from .test_server import fake_sems, listening

FORWARD = Settings(forward_to_cloud=True)


def ack_for(frame, payload=bytes(16)):
    """Answer a long-envelope device frame like SEMS does."""
    return reply_packet(frame[14:22], frame[22:30], kind=frame[11], payload=payload)


async def send_and_close(port, *frames):
    """Mimic a device that opens one TCP connection per report."""
    async with tcp_client(port) as (reader, writer):
        writer.write(b"".join(frames))
        writer.write_eof()
        return await asyncio.wait_for(reader.read(), 3)


def acknowledging(received, delay=0):
    """Create an upstream that records every frame and acknowledges it."""

    async def respond(reader, writer):
        while frame := await read_frame(reader, DEVICE_PREFIX):
            received.append(frame)
            await asyncio.sleep(delay)
            if frame[11] != 0x10:
                writer.write(ack_for(frame))
                await writer.drain()

    return respond


def connect_and_send(port, data):
    """Use a plain socket, so closing it can send a real TCP reset on every platform."""
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(data)
    return sock


def reset(sock):
    linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
    sock.close()


# --- SEMS replies for the DNS G3 inverter ------------------------------------


def test_genuine_dnsg3_time_sync_acknowledgement_is_recognised():
    request = decode_device(captured_body("dnsg3_time_sync_body", 1, 0x03))
    assert classify_reply(capture("dnsg3_time_sync_server_ack"), request) == "ack"


def test_documented_time_set_is_accepted_for_the_inverter():
    request = decode_device(captured_body("dnsg3_time_sync_body", 1, 0x03))
    assert classify_reply(time_set_packet(b"53000DSC", b"01234567"), request) == "time_sync"


@pytest.mark.parametrize("with_time_set", [False, True])
async def test_dnsg3_time_sync_exchange(monkeypatch, with_time_set):
    monkeypatch.setattr(relay, "FOLLOWUP_TIMEOUT", 0.1, raising=False)
    monkeypatch.setattr(relay, "REPLY_TIMEOUT", 0.5)
    request = captured_body("dnsg3_time_sync_body", 1, 0x03)
    replies = capture("dnsg3_time_sync_server_ack")
    if with_time_set:
        replies += time_set_packet(b"53000DSC", b"01234567")

    async def respond(reader, writer):
        await read_frame(reader, DEVICE_PREFIX)
        writer.write(replies)
        await writer.drain()
        await reader.read()

    async with fake_sems(monkeypatch, respond), listening(FORWARD) as (runtime, port):
        assert await send_and_close(port, request) == replies
        device = runtime.devices["01234567"]
        assert device.cloud_health == "healthy"
        assert runtime.counters["reply_timeout"] == 0


async def test_meter_time_sync_with_only_an_acknowledgement_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(relay, "FOLLOWUP_TIMEOUT", 0.1, raising=False)
    monkeypatch.setattr(relay, "REPLY_TIMEOUT", 0.5)
    received = []
    async with (
        fake_sems(monkeypatch, acknowledging(received)),
        listening(FORWARD) as (
            runtime,
            port,
        ),
    ):
        await send_and_close(port, capture("hk1000_time_sync"))
        assert runtime.devices["01234567"].cloud_health == "healthy"
        assert runtime.counters["reply_timeout"] == 0


@pytest.mark.parametrize("name", ["hk1000_time_sync_client_ack", "dnsg3_time_sync_client_ack"])
async def test_published_device_time_set_acknowledgements_are_forwarded(monkeypatch, name):
    received = []
    async with (
        fake_sems(monkeypatch, acknowledging(received)),
        listening(FORWARD) as (
            _,
            port,
        ),
    ):
        await send_and_close(port, capture(name))
        assert received == [capture(name)]


# --- Traffic must not be silently dropped --------------------------------------


async def test_wellformed_frames_are_forwarded_even_when_not_decoded_locally(monkeypatch):
    monkeypatch.setattr(relay, "FOLLOWUP_TIMEOUT", 0.05, raising=False)
    frames = [
        # A DNS G3 power rating this integration has never seen.
        device_packet(b"55000DSC", payload=bytes(0x1F0)),
        # An undocumented message type from a known meter.
        device_packet(kind=0x16, payload=bytes(32)),
        # Telemetry rejected locally: blank meter sample and short DT backfill.
        device_packet(payload=meter_payload(voltage=0)),
        device_packet(b"58000DTA", b"INV00001", kind=0x45, payload=bytes(0x60)),
    ]
    corrupt = device_packet()[:-1] + bytes([device_packet()[-1] ^ 1])
    received = []
    async with (
        fake_sems(monkeypatch, acknowledging(received)),
        listening(FORWARD) as (
            runtime,
            port,
        ),
    ):
        for frame in (*frames, corrupt):
            await send_and_close(port, frame)
        assert received == frames
        assert runtime.counters["telemetry"] == 0
        assert all(device.payload is None for device in runtime.devices.values())


async def test_published_dnsg3_metrics_and_backfill_are_forwarded(monkeypatch):
    frames = [
        captured_body("dnsg3_metrics_body", 1, 0x04),
        captured_body("dnsg3_backfill_body", 1, 0x45),
    ]
    received = []
    async with (
        fake_sems(monkeypatch, acknowledging(received)),
        listening(FORWARD) as (
            runtime,
            port,
        ),
    ):
        replies = await send_and_close(port, *frames)
        assert received == frames
        assert replies == b"".join(ack_for(frame) for frame in frames)
        assert runtime.devices["01234567"].payload is None


async def test_undocumented_reply_is_counted_and_not_delivered(monkeypatch):
    monkeypatch.setattr(relay, "REPLY_TIMEOUT", 0.5)

    async def respond(reader, writer):
        frame = await read_frame(reader, DEVICE_PREFIX)
        writer.write(reply_packet(kind=0x30) + ack_for(frame))
        await writer.drain()

    async with fake_sems(monkeypatch, respond), listening(FORWARD) as (runtime, port):
        frame = device_packet()
        assert await send_and_close(port, frame) == ack_for(frame)
        assert runtime.counters["cloud_replies_dropped"] == 1
        assert runtime.devices["TEST0001"].counters["cloud_replies_dropped"] == 1


# --- Backoff, reconnects and connection loss ----------------------------------


async def test_backoff_applies_across_new_device_connections(monkeypatch):
    attempts = []

    async def refuse(_endpoint):
        attempts.append(1)
        raise RelayFailure("connect_failed")

    monkeypatch.setattr(RelayEndpoint, "connect", refuse)
    async with listening(FORWARD) as (runtime, port):
        for index in range(5):
            await send_and_close(port, device_packet(sent=stamp(second=index)))
        assert len(attempts) == 1
        assert runtime.counters["retry_backoff"] == 4
        assert runtime.devices["TEST0001"].cloud_health == "connect_failed"
        assert runtime.counters["telemetry"] == 5


async def test_backoff_grows_is_capped_and_resets_after_success(monkeypatch):
    clock = [1000.0]
    endpoint = RelayEndpoint(FORWARD, set())
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock[0])
    delays = []
    for _ in range(5):
        endpoint.failed(None, "connect_failed")
        endpoint.failed(None, "connect_failed")  # A concurrent failure cannot extend the pause.
        deadline = endpoint._backoff[None][0]
        delays.append(deadline - clock[0])
        endpoint.succeeded(b"other")  # A reply on an already-open socket cannot clear it.
        assert endpoint.backoff_reason(b"device") == "connect_failed"
        clock[0] = deadline
    assert delays == [10, 20, 40, 60, 60]
    endpoint.connected()
    endpoint.failed(None, "dns_failure")
    assert endpoint.backoff_reason(b"device") == "dns_failure"
    assert endpoint._backoff[None][0] - clock[0] == 10


async def test_recovery_after_backoff_expires(monkeypatch):
    monkeypatch.setattr(relay, "RETRY_DELAY", 0.05)
    received = []
    async with fake_sems(monkeypatch, acknowledging(received)):
        working = RelayEndpoint.connect
        outage = [True]

        async def connect(endpoint):
            if outage:
                outage.pop()
                raise RelayFailure("connect_failed")
            return await working(endpoint)

        monkeypatch.setattr(RelayEndpoint, "connect", connect)
        async with listening(FORWARD) as (runtime, port):
            await send_and_close(port, device_packet(sent=stamp(second=1)))
            await asyncio.sleep(0.08)
            later = device_packet(sent=stamp(second=2))
            assert await send_and_close(port, later) == ack_for(later)
            assert runtime.devices["TEST0001"].cloud_health == "healthy"


async def test_endpoint_backoff_reports_outage_and_recovery_for_other_devices(monkeypatch):
    received = []
    async with fake_sems(monkeypatch, acknowledging(received)):
        working = RelayEndpoint.connect
        offline = False

        async def connect(endpoint):
            if offline:
                raise RelayFailure("dns_failure")
            return await working(endpoint)

        monkeypatch.setattr(RelayEndpoint, "connect", connect)
        async with listening(FORWARD) as (runtime, port):
            await send_and_close(port, device_packet(serial=b"OTHERMTR"))
            device = runtime.devices["OTHERMTR"]
            assert device.cloud_health == "healthy"
            initial_success = device.last_cloud_success
            offline = True
            await send_and_close(port, device_packet())
            await send_and_close(port, device_packet(serial=b"OTHERMTR", sent=stamp(second=1)))
            assert device.cloud_health == "dns_failure"
            assert device.last_cloud_success == initial_success
            assert device.counters["retry_backoff"] == 1
            assert device.counters["telemetry"] == 2
            offline = False
            monkeypatch.setattr(runtime.endpoint, "backoff_reason", lambda _identity: None)
            await send_and_close(port, device_packet(serial=b"OTHERMTR", sent=stamp(second=2)))
            assert device.cloud_health == "healthy"
            assert device.counters["cloud_success"] == 2


async def test_local_device_limit_does_not_drop_intact_relay_frames(monkeypatch):
    monkeypatch.setattr(server, "MAX_DEVICES", 1)
    received = []
    frames = [
        device_packet(),
        device_packet(serial=b"OTHERMTR"),
        device_packet(b"93000HKU"),
        device_packet(serial=b"OTHERMTR", sent=stamp(second=1)),
    ]
    async with (
        fake_sems(monkeypatch, acknowledging(received)),
        listening(FORWARD) as (
            runtime,
            port,
        ),
    ):
        await send_and_close(port, *frames[:2])
        await send_and_close(port, *frames[2:])
        assert received == frames
        assert set(runtime.devices) == {"TEST0001"}
        assert runtime.counters["identity_changes"] == 1
        assert runtime.counters["conflicting_devices"] == 1
        assert runtime.counters["devices_rejected"] == 1


async def test_one_unanswered_device_does_not_block_another(monkeypatch):
    monkeypatch.setattr(relay, "REPLY_TIMEOUT", 0.15)

    async def respond(reader, writer):
        while frame := await read_frame(reader, DEVICE_PREFIX):
            if frame[22:30] != b"SILENT01":
                writer.write(ack_for(frame))
                await writer.drain()

    async with fake_sems(monkeypatch, respond), listening(FORWARD) as (runtime, port):
        await send_and_close(port, device_packet(serial=b"SILENT01"))
        healthy = device_packet()
        assert await send_and_close(port, healthy) == ack_for(healthy)
        assert runtime.devices["SILENT01"].cloud_health == "reply_timeout"
        assert runtime.devices["TEST0001"].cloud_health == "healthy"


async def test_cloud_rejection_does_not_start_a_backoff(monkeypatch):
    async def reject(reader, writer):
        while frame := await read_frame(reader, DEVICE_PREFIX):
            writer.write(ack_for(frame, b"\x02" + bytes(15)))
            await writer.drain()

    async with fake_sems(monkeypatch, reject), listening(FORWARD) as (runtime, port):
        for index in range(3):
            await send_and_close(port, device_packet(sent=stamp(second=index)))
        assert runtime.counters["cloud_rejected"] == 3
        assert runtime.counters["retry_backoff"] == 0


@pytest.mark.parametrize("ending", ["reset", "idle"])
async def test_accepted_frames_are_forwarded_after_connection_loss(monkeypatch, ending):
    if ending == "idle":
        monkeypatch.setattr(server, "FRAME_TIMEOUT", 0.1)
    received = []
    frames = [device_packet(sent=stamp(second=index)) for index in range(3)]
    upstream = acknowledging(received, delay=0.1)
    async with fake_sems(monkeypatch, upstream), listening(FORWARD) as (runtime, port):
        sock = connect_and_send(port, b"".join(frames))
        try:
            await until(lambda: runtime.counters["telemetry"] == 3)
            if ending == "reset":
                reset(sock)
            await until(lambda: len(received) == 3, timeout=3)
        finally:
            sock.close()
        assert received == frames
        error = "connection_errors" if ending == "reset" else "read_timeouts"
        assert runtime.counters[error] == 1
        assert runtime.counters["upstream_disconnected"] == 0


async def test_device_that_left_is_not_reported_as_an_upstream_failure(monkeypatch):
    received = []
    upstream = acknowledging(received, delay=0.15)
    async with fake_sems(monkeypatch, upstream), listening(FORWARD) as (runtime, port):
        sock = connect_and_send(port, device_packet())
        await until(lambda: received)
        reset(sock)
        device = runtime.devices["TEST0001"]
        await until(lambda: device.cloud_health != "waiting")
        assert device.cloud_health == "healthy"
        assert runtime.counters["connection_errors"] == 1
        assert runtime.counters["upstream_disconnected"] == 0
        await until(lambda: runtime.counters["reply_undeliverable"] == 1)


async def test_burst_of_buffered_backfill_is_forwarded_in_order(monkeypatch):
    frames = [device_packet(kind=0x45, sent=stamp(minute=index)) for index in range(60)]
    received = []
    async with (
        fake_sems(monkeypatch, acknowledging(received)),
        listening(FORWARD) as (
            runtime,
            port,
        ),
    ):
        await send_and_close(port, *frames)
        assert received == frames
        assert runtime.counters["queue_full"] == 0


async def test_unexpected_relay_error_is_recorded_and_forwarding_continues(monkeypatch):
    original = relay.classify_reply
    failures = [RuntimeError("private detail")]

    def flaky(frame, request):
        if failures:
            raise failures.pop()
        return original(frame, request)

    monkeypatch.setattr(relay, "classify_reply", flaky)
    received = []
    frames = [device_packet(sent=stamp(second=index)) for index in range(2)]
    async with (
        fake_sems(monkeypatch, acknowledging(received)),
        listening(FORWARD) as (
            runtime,
            port,
        ),
    ):
        assert await send_and_close(port, *frames) == ack_for(frames[1])
        assert runtime.counters["internal_errors"] == 1
        device = runtime.devices["TEST0001"]
        assert device.counters["relay_error"] == 1
        assert device.cloud_health == "healthy"


async def test_slow_address_gets_a_usable_share_of_the_connect_budget(monkeypatch):
    async def resolve(_host, port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"8.8.8.{index}", port))
            for index in range(1, 9)
        ]

    async def slow_connect(*_args, **_kwargs):
        await asyncio.sleep(1.2)
        return "reader", "writer"

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(asyncio, "open_connection", slow_connect)
    assert await RelayEndpoint(FORWARD, set()).connect() == ("reader", "writer")


async def test_local_adapter_address_is_blocked_in_any_notation(monkeypatch):
    async def resolve(_host, port, **_kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", port, 0, 0))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    endpoint = RelayEndpoint(FORWARD, {"2606:4700:0000:0000:0000:0000:0000:1111"})
    with pytest.raises(RelayFailure, match="blocked_address"):
        await endpoint.connect()


async def test_forwarding_outage_and_recovery_are_logged_once_without_identity(monkeypatch, caplog):
    monkeypatch.setattr(relay, "RETRY_DELAY", 0.01)
    caplog.set_level(logging.INFO, "custom_components.goodwe_local_server")
    received = []
    async with fake_sems(monkeypatch, acknowledging(received)):
        working = RelayEndpoint.connect
        outage = [True, True]

        async def connect(endpoint):
            if outage:
                outage.pop()
                raise RelayFailure("connect_failed")
            return await working(endpoint)

        monkeypatch.setattr(RelayEndpoint, "connect", connect)
        async with listening(FORWARD) as (_, port):
            for index in range(4):
                await send_and_close(port, device_packet(sent=stamp(second=index)))
                await asyncio.sleep(0.05)
    messages = [record.getMessage() for record in caplog.records]
    assert sum("forwarding is failing" in message for message in messages) == 1
    assert sum("forwarding recovered" in message for message in messages) == 1
    assert "TEST0001" not in caplog.text


# --- Resource limits -----------------------------------------------------------


async def test_one_host_cannot_take_every_connection_slot(monkeypatch):
    monkeypatch.setattr(server, "MAX_CONNECTIONS_PER_PEER", 2, raising=False)
    async with listening() as (runtime, port):
        async with tcp_client(port), tcp_client(port):
            await until(lambda: runtime.counters["connections"] == 2)
            async with tcp_client(port) as (rejected, _):
                assert await asyncio.wait_for(rejected.read(), 1) == b""
        assert runtime.counters["connections_rejected"] == 1
        await until(lambda: not runtime._clients)
        await send_and_close(port, device_packet())
        assert runtime.counters["telemetry"] == 1


async def test_buffered_frames_yield_to_the_event_loop():
    runtime = server.GoodWeServer(Settings(), set())
    reader = asyncio.StreamReader()
    reader.feed_data(b"".join(device_packet(sent=stamp(second=i)) for i in range(20)))
    reader.feed_eof()

    class Writer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    ticks = []

    async def ticker():
        while True:
            ticks.append(runtime.counters["telemetry"])
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    await runtime._handle(reader, Writer(), None)
    task.cancel()
    await runtime.stop()
    assert len(set(ticks)) > 10


# --- Freshness, replay and statistics -----------------------------------------


async def test_repeated_frame_does_not_refresh_availability():
    frame = device_packet(sent=stamp(10, 0, 0))
    reencrypted = device_packet(sent=stamp(10, 0, 0), iv=b"x" * 16)
    assert frame != reencrypted
    async with listening(Settings(stale_after=0.2)) as (runtime, port):
        await send_and_close(port, frame)
        device = runtime.devices["TEST0001"]
        first = device.received_at
        await asyncio.sleep(0.05)
        await send_and_close(port, reencrypted)
        assert device.received_at == first
        assert device.counters["duplicate_telemetry"] == 1
        await until(lambda: not device.fresh)
        await send_and_close(port, frame)
        assert not device.fresh


async def test_older_measurement_does_not_replace_a_newer_one():
    newer = device_packet(payload=meter_payload(-900), sent=stamp(10, 5))
    older = [
        device_packet(payload=meter_payload(-100 - index), sent=stamp(10, 0, index))
        for index in range(4)
    ]
    async with listening() as (runtime, port):
        await send_and_close(port, newer, *older)
        device = runtime.devices["TEST0001"]
        assert device.payload == meter_payload(-900)
        assert device.counters["out_of_order_telemetry"] == 4
        assert device.counters["telemetry"] == 1


@pytest.fixture
async def advance_receipt_clock(monkeypatch):
    loop = asyncio.get_running_loop()
    monotonic = loop.time
    offset = 0

    def advance(seconds):
        nonlocal offset
        offset += seconds

    monkeypatch.setattr(loop, "time", lambda: monotonic() + offset)
    return advance


async def test_delayed_live_reports_cannot_rebase_a_fresh_clock(advance_receipt_clock):
    async with listening() as (runtime, port):
        await send_and_close(port, device_packet(sent=stamp(10)))
        device = runtime.devices["TEST0001"]
        for second in (0, 30):
            advance_receipt_clock(30)
            await send_and_close(
                port, device_packet(payload=meter_payload(-1), sent=stamp(9, 0, second))
            )
        assert device.payload == meter_payload()
        assert device.counters["out_of_order_telemetry"] == 2


@pytest.mark.parametrize("corrected_hour", [9, 11, 0])
async def test_clock_change_recovers_after_stale_with_paced_reports(
    advance_receipt_clock, corrected_hour
):
    async with listening() as (runtime, port):
        await send_and_close(port, device_packet(sent=stamp(10)))
        device = runtime.devices["TEST0001"]
        advance_receipt_clock(151)
        await until(lambda: not device.fresh)
        # Catch-up reports received in a burst don't prove that the clock moved.
        await send_and_close(
            port,
            *(device_packet(sent=stamp(corrected_hour, minute)) for minute in range(3)),
        )
        assert not device.fresh
        # A lower lifetime count indicates old measurements even if they arrive on cadence.
        for minute in range(3, 6):
            advance_receipt_clock(60)
            await send_and_close(
                port,
                device_packet(
                    sent=stamp(corrected_hour, minute), payload=meter_payload(import_energy=1)
                ),
            )
        assert not device.fresh
        for minute in range(6, 9):
            advance_receipt_clock(60)
            await send_and_close(
                port, device_packet(sent=stamp(corrected_hour, minute), payload=meter_payload(-2))
            )
        assert device.payload == meter_payload(-2)
        assert device.fresh
        assert device.counters["telemetry_clock_rebased"] == 1
        # Once rebased, an isolated report from the old clock cannot restore that timeline.
        await send_and_close(port, device_packet(sent=stamp(10, 10)))
        assert device.payload == meter_payload(-2)


async def test_invalid_far_future_and_isolated_clock_jump_do_not_poison_ordering():
    async with listening() as (runtime, port):
        invalid = device_packet(sent=bytes(6))
        future = device_packet(sent=bytes([99, 1, 1, 0, 0, 0]))
        await send_and_close(port, invalid, future)
        device = runtime.devices["TEST0001"]
        assert device.payload is None
        assert device.counters["invalid_telemetry_timestamp"] == 1
        assert device.counters["future_telemetry_timestamp"] == 1
        await send_and_close(
            port,
            device_packet(sent=stamp(10)),
            device_packet(sent=stamp(11)),
            device_packet(sent=stamp(10, 0, 30), payload=meter_payload(-400)),
        )
        assert device.payload == meter_payload(-400)
        assert device.counters["telemetry_clock_jump"] == 1
        assert device.counters["telemetry"] == 2


async def test_initial_clock_ahead_recovers_without_assuming_a_device_timezone(
    advance_receipt_clock,
):
    async with listening() as (runtime, port):
        await send_and_close(port, device_packet(sent=stamp(16)))
        device = runtime.devices["TEST0001"]
        advance_receipt_clock(151)
        await until(lambda: not device.fresh)
        for second in (0, 30):
            await send_and_close(port, device_packet(sent=stamp(10, 0, second)))
            advance_receipt_clock(30)
            assert not device.fresh
        await send_and_close(port, device_packet(sent=stamp(10, 1), payload=meter_payload(-2)))
        assert device.fresh
        assert device.payload == meter_payload(-2)


async def test_outage_with_unchanged_clock_accepts_current_sample_after_backfill(
    advance_receipt_clock,
):
    async with listening() as (runtime, port):
        await send_and_close(port, device_packet(sent=stamp(10)))
        device = runtime.devices["TEST0001"]
        advance_receipt_clock(600)
        await until(lambda: not device.fresh)
        await send_and_close(
            port,
            device_packet(sent=stamp(10, 1)),
            device_packet(kind=0x45, sent=stamp(10, 10)),
        )
        assert not device.fresh
        await send_and_close(port, device_packet(sent=stamp(10, 10), payload=meter_payload(-400)))
        assert device.fresh
        assert device.payload == meter_payload(-400)
        assert device.counters["telemetry"] == 2


def _read(descriptions, payload, settings=Settings()):
    return {d.key: d.read(payload, settings) for d in descriptions}


@pytest.mark.parametrize("ct2", [-101, -3898])
def test_large_negative_inverter_ct_is_not_reported_as_zero_generation(ct2):
    # Values published by an HK1000 owner show CT2 reading -3898 W under load.
    payload = meter_payload(-3630, ct2)
    readings = _read(HK1000_READINGS, payload)
    assert readings["solar_generation_power"] is None
    assert readings["household_consumption_power"] is None
    assert readings["grid_import_power"] == 3630
    assert _read(METER_REGISTERS, payload)["l2_export_power"] == ct2


def test_small_standby_draw_is_still_zero_generation():
    readings = _read(HK1000_READINGS, meter_payload(-1600, -100))
    assert readings["solar_generation_power"] == 0
    assert readings["household_consumption_power"] == 1600


def test_default_timeout_tolerates_one_missed_sixty_second_report():
    assert Settings().stale_after == 150


async def test_shutdown_interrupts_forwarding_that_outlived_its_connection(monkeypatch):
    waiting = asyncio.Event()

    async def stall(reader, _writer):
        await read_frame(reader, DEVICE_PREFIX)
        waiting.set()
        await reader.read()

    async with fake_sems(monkeypatch, stall), listening(FORWARD) as (runtime, port):
        async with tcp_client(port) as (reader, writer):
            writer.write(device_packet())
            writer.write_eof()
            await asyncio.wait_for(waiting.wait(), 1)
            await asyncio.wait_for(runtime.stop(), 1)
            assert await asyncio.wait_for(reader.read(), 1) == b""
        assert not runtime._clients
        assert not runtime.endpoint.in_flight


async def test_drain_deadline_releases_a_stalled_upstream(monkeypatch):
    monkeypatch.setattr(server, "RELAY_DRAIN_TIMEOUT", 0.12)
    upstream_closed = asyncio.Event()

    async def stall(reader, _writer):
        assert await read_frame(reader, DEVICE_PREFIX) is not None
        assert await reader.read() == b""
        upstream_closed.set()

    async with fake_sems(monkeypatch, stall), listening(FORWARD) as (runtime, port):
        await send_and_close(port, device_packet(), device_packet(sent=stamp(second=1)))
        await asyncio.wait_for(upstream_closed.wait(), 1)
        device = runtime.devices["TEST0001"]
        assert device.counters["relay_drain_timeout"] == 1
        assert device.counters["telemetry"] == 2
        assert not runtime.endpoint.in_flight


async def test_invalid_frame_on_a_new_connection_is_attributed_to_its_device():
    async with listening() as (runtime, port):
        await send_and_close(port, device_packet())
        await send_and_close(port, device_packet(payload=meter_payload(voltage=0)))
        device = runtime.devices["TEST0001"]
        assert device.counters["invalid_packets"] == 1
        assert device.payload == meter_payload()


async def test_loop_guard_only_judges_the_first_frame_of_a_connection(monkeypatch):
    monkeypatch.setattr(relay, "REPLY_TIMEOUT", 0.5)
    unknown = device_packet(b"55000DSC", payload=bytes(0x70))
    waiting = asyncio.Event()

    async def stall(reader, _writer):
        await read_frame(reader, DEVICE_PREFIX)
        waiting.set()
        await reader.read()

    async with fake_sems(monkeypatch, stall), listening(FORWARD) as (runtime, port):
        async with tcp_client(port) as (_, writer):
            writer.write(unknown)
            await asyncio.wait_for(waiting.wait(), 1)
            # The same unrecognized frame repeated on its own connection is not a loop.
            writer.write(unknown)
            await until(lambda: runtime.counters["unsupported_packets"] == 2)
            assert runtime.counters["relay_loop"] == 0
