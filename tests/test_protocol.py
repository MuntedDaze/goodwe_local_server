"""Protocol framing and register checks against captures and synthetic samples."""

import asyncio

import pytest

from custom_components.goodwe_local_server.const import Settings
from custom_components.goodwe_local_server.protocol import (
    CLOUD_PREFIX,
    DEVICE_PREFIX,
    ProtocolError,
    classify_reply,
    crc16,
    decode_device,
    read_frame,
)
from custom_components.goodwe_local_server.sensor import (
    DT_REGISTERS,
    HK1000_READINGS,
    METER_REGISTERS,
)

from .helpers import capture, device_packet, meter_payload, reply_packet


def _decode(descriptions, payload, settings=Settings()):
    return {description.key: description.read(payload, settings) for description in descriptions}


def test_known_hk1000_capture():
    packet = decode_device(capture("hk1000_capture"))
    assert (packet.model, packet.serial) == ("91000HKU", "01234567")
    assert classify_reply(capture("hk1000_ack"), packet) == "ack"
    assert crc16(b"123456789") == 0x4B37
    readings = _decode(HK1000_READINGS, packet.telemetry)
    assert readings == pytest.approx(
        {
            "signed_grid_power": -1557,
            "grid_import_power": 0,
            "grid_export_power": 1557,
            "solar_generation_power": 2601,
            "household_consumption_power": 1044,
            "lifetime_grid_import_energy": 801.55,
            "lifetime_grid_export_energy": 270.78,
            "lifetime_solar_generation_energy": 579.41,
            "grid_voltage": 243.8,
        }
    )
    legacy = _decode(METER_REGISTERS, packet.telemetry)
    assert legacy["total_export_power"] == 4159
    assert legacy["total_energy_exported"] == pytest.approx(783.40)


@pytest.mark.parametrize(
    ("grid", "solar", "offset", "generation", "household"),
    [
        (-1600, 5, 0, 5, 1605),
        (-1600, 5, 8, 0, 1600),
        (-1600, 9, 8, 9, 1609),
        (-1600, -10, 8, 0, 1600),
        (50, 12, 0, 12, None),
    ],
)
def test_hk1000_import_and_solar_offset(grid, solar, offset, generation, household):
    payload = meter_payload(grid, solar)
    readings = _decode(HK1000_READINGS, payload, Settings(solar_offset_w=offset))
    assert _decode(METER_REGISTERS, payload)["l2_export_power"] == solar
    assert readings["solar_generation_power"] == generation
    assert readings["household_consumption_power"] == household
    assert readings["lifetime_grid_import_energy"] == 320.0
    assert readings["grid_export_power"] == max(0, grid)
    assert readings["grid_import_power"] == max(0, -grid)


def test_negative_energy_is_not_reported_as_zero_or_reinterpreted():
    payload = bytearray(meter_payload())
    payload[0x07:0x0B] = b"\xff" * 4
    payload[0x1F:0x23] = (2**31 - 1).to_bytes(4)
    readings = _decode(HK1000_READINGS, payload)
    assert readings["lifetime_grid_export_energy"] is None
    assert readings["lifetime_grid_import_energy"] == pytest.approx(21474836.47)
    assert _decode(METER_REGISTERS, payload)["l1_energy_exported"] == pytest.approx(-0.01)


def test_hk3000_and_dt_keep_legacy_layout():
    meter = decode_device(device_packet(b"93000HKU", payload=meter_payload(-1320, 450)))
    legacy = _decode(METER_REGISTERS, meter.telemetry)
    assert legacy["total_export_power"] == -870
    assert legacy["l1_peak_current"] == pytest.approx(0.84)
    assert legacy["total_energy_imported"] == pytest.approx(9999.99)

    payload = bytearray(0x110)
    for offset, length, value in (
        (0x45, 2, 4998),
        (0x4B, 4, 1120),
        (0x67, 2, -34),
        (0x6D, 2, 15),
        (0x6F, 4, 23892),
        (0x73, 4, 1052),
    ):
        payload[offset : offset + length] = value.to_bytes(length, signed=True)
    inverter = decode_device(device_packet(b"58000DTA", payload=bytes(payload)))
    readings = _decode(DT_REGISTERS, inverter.telemetry)
    assert {
        key: readings[key]
        for key in (
            "total_power",
            "l1_frequency",
            "temperature",
            "todays_energy",
            "total_energy",
            "runtime",
        )
    } == pytest.approx(
        {
            "total_power": 1120,
            "l1_frequency": 49.98,
            "temperature": -3.4,
            "todays_energy": 1.5,
            "total_energy": 2389.2,
            "runtime": 1052,
        }
    )
    short_inverter = decode_device(device_packet(b"58000DTA", payload=bytes(payload[:0x80])))
    assert _decode(DT_REGISTERS, short_inverter.telemetry)["0xcd_0x1ff"] is None


@pytest.mark.parametrize(
    ("frame", "error"),
    [
        (capture("hk1000_capture")[:-1] + b"\0", "bad checksum"),
        (device_packet(serial=b"BAD\xff0001"), "invalid model or serial"),
        (device_packet(payload=bytes(0x60)), "invalid HomeKit telemetry length"),
        (device_packet(payload=meter_payload(voltage=0)), "missing HomeKit line voltage"),
        (device_packet(b"58000DTA", payload=bytes(0x70)), "short DT inverter telemetry"),
        (device_packet(kind=0x10, payload=bytes(32)), "invalid time sync acknowledgement length"),
    ],
)
def test_invalid_device_frames(frame, error):
    with pytest.raises(ProtocolError, match=error):
        decode_device(frame)


def test_unsupported_models_and_backfill_are_not_live_telemetry():
    assert decode_device(device_packet(b"UNKNOWN1")) is None
    assert decode_device(device_packet(kind=0x16)) is None
    assert decode_device(device_packet(kind=0x45)).telemetry is None
    assert decode_device(device_packet(b"53000DSC")).telemetry is None


async def test_frame_reads_fragments_and_coalesced_frames():
    reader = asyncio.StreamReader()
    frame = capture("hk1000_capture")
    pending = asyncio.create_task(read_frame(reader, DEVICE_PREFIX))
    for index in range(len(frame)):
        reader.feed_data(frame[index : index + 1])
        await asyncio.sleep(0)
    assert await pending == frame
    reader.feed_data(frame + frame)
    reader.feed_eof()
    assert await read_frame(reader, DEVICE_PREFIX) == frame
    assert await read_frame(reader, DEVICE_PREFIX) == frame
    assert await read_frame(reader, DEVICE_PREFIX) is None


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"POST", "truncated prefix"),
        (b"POSTGW\0\0", "truncated frame"),
        (b"POSTGW\0\0\0\xf0\0", "truncated frame"),
        (b"POSTGW\xff\xff\xff\xff", "invalid frame length"),
        (b"POSTGW\0\0\0\0", "invalid frame length"),
        (b"garbag", "invalid prefix"),
    ],
)
async def test_invalid_stream_boundaries(data, error):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    with pytest.raises(ProtocolError, match=error):
        await read_frame(reader, DEVICE_PREFIX)


async def test_cloud_time_sync_pair_keepalive_and_reply_filtering():
    request = decode_device(capture("hk1000_time_sync"))
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x01\x02" + capture("hk1000_time_sync_replies"))
    reader.feed_eof()
    assert classify_reply(await read_frame(reader, CLOUD_PREFIX), request) is None
    assert classify_reply(await read_frame(reader, CLOUD_PREFIX), request) == "ack"
    assert classify_reply(await read_frame(reader, CLOUD_PREFIX), request) == "time_sync"
    assert await read_frame(reader, CLOUD_PREFIX) is None
    metrics = decode_device(device_packet())
    assert classify_reply(reply_packet(serial=b"OTHER001"), metrics) is None
    assert classify_reply(reply_packet(kind=0x30), metrics) is None
    assert classify_reply(reply_packet(payload=b"\x02" + bytes(15)), metrics) == "nack"
    assert classify_reply(reply_packet(payload=b"\x01" + bytes(15)), metrics) is None
