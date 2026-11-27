"""Framing and decoding for the GoodWe SEMS telemetry TCP protocol."""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DEVICE_PREFIX = b"POSTGW"
CLOUD_PREFIX = b"GW"
CLOUD_KEEPALIVE = b"\x01\x02"
MAX_FRAME = 4096
METER_MODELS = {"91000HKU": "HK1000", "93000HKU": "HK3000"}
INVERTER_MODEL = "58000DTA"
# The DNS G3 has its own layout. It is forwarded to SEMS and never decoded locally.
RELAY_ONLY_MODEL = "53000DSC"
DEVICE_FAMILY = dict.fromkeys(METER_MODELS, 3) | {
    INVERTER_MODEL: 1,
    RELAY_ONLY_MODEL: 1,
}
# Message types that SEMS answers, and the device's one-way clock acknowledgement.
REQUEST_KINDS = (0x03, 0x04, 0x45)
CLOCK_ACK_KIND = 0x10
# The published SEMS clock message. Its family byte is 1 even for meters.
TIME_SET = (0x01, 0x16)
_KEY = b"\xff" * 16
_IDENTITY = re.compile(rb"[0-9A-Za-z]{16}")
ACK = bytes(16)
NACK = b"\x02" + bytes(15)


class ProtocolError(ValueError):
    """A frame failed validation."""


async def read_frame(reader: asyncio.StreamReader, prefix: bytes) -> bytes | None:
    """Read one bounded frame, returning None at a clean frame boundary on EOF."""
    try:
        first = await reader.readexactly(len(prefix))
    except asyncio.IncompleteReadError as err:
        if not err.partial:
            return None
        raise ProtocolError("truncated prefix") from None
    if first == CLOUD_KEEPALIVE and prefix == CLOUD_PREFIX:
        return first
    if first != prefix:
        raise ProtocolError("invalid prefix")
    try:
        length = await reader.readexactly(4)
        # The encoded length omits the CRC and undercounts the body by one byte.
        body_size = int.from_bytes(length) + 3
        if not 4 <= body_size <= MAX_FRAME - len(prefix) - 4:
            raise ProtocolError("invalid frame length")
        return first + length + await reader.readexactly(body_size)
    except asyncio.IncompleteReadError:
        raise ProtocolError("truncated frame") from None


def crc16(data: bytes) -> int:
    """Compute the Modbus CRC-16 over the header and body."""
    result = 0xFFFF
    for octet in data:
        result ^= octet
        for _ in range(8):
            result = (result >> 1) ^ (0xA001 if result & 1 else 0)
    return result


def _validate_crc(frame: bytes, byteorder: str) -> None:
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], byteorder):
        raise ProtocolError("bad checksum")


def _decrypt(iv: bytes, ciphertext: bytes) -> bytes:
    if not ciphertext or len(ciphertext) % 16:
        raise ProtocolError("invalid encrypted payload length")
    decryptor = Cipher(algorithms.AES(_KEY), modes.CBC(iv)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


@dataclass(frozen=True, slots=True)
class DeviceFrame:
    """A checksummed device frame, plus whatever could be decoded locally."""

    raw: bytes
    family: int
    kind: int
    # The 16 bytes of model and serial that SEMS echoes in its replies.
    identity: bytes
    model: str = ""
    serial: str = ""
    telemetry: bytes | None = None
    # Envelope timestamp (Y M D H m s). Big-endian bytes sort chronologically.
    sent_at: bytes = b""


def _identity_offset(kind: int) -> int:
    return 12 if kind == CLOCK_ACK_KIND else 14


def parse_device(frame: bytes) -> DeviceFrame:
    """Check a framed device message is intact, without interpreting its content."""
    _validate_crc(frame, "big")
    family, kind = frame[10:12]
    offset = _identity_offset(kind)
    identity = frame[offset : offset + 16] if len(frame) >= offset + 18 else b""
    return DeviceFrame(frame, family, kind, identity)


def decode_telemetry(message: DeviceFrame) -> DeviceFrame | None:
    """Decode a recognized message, returning None for unsupported types and models."""
    frame, family, kind = message.raw, message.family, message.kind
    if kind not in (*REQUEST_KINDS, CLOCK_ACK_KIND) or family not in (1, 3):
        return None
    short_envelope = kind == CLOCK_ACK_KIND
    identity = message.identity
    if not _IDENTITY.fullmatch(identity):
        raise ProtocolError("invalid model or serial")
    model, serial = identity[:8].decode("ascii"), identity[8:].decode("ascii")
    if DEVICE_FAMILY.get(model) != family:
        return None
    iv_offset = _identity_offset(kind) + 16
    payload_offset = iv_offset + (16 if short_envelope else 22)
    payload = _decrypt(frame[iv_offset : iv_offset + 16], frame[payload_offset:-2])
    if short_envelope and len(payload) != 16:
        raise ProtocolError("invalid time sync acknowledgement length")
    if kind in (0x04, 0x45):
        if family == 3 and len(payload) != 0x70:
            raise ProtocolError("invalid HomeKit telemetry length")
        if model == INVERTER_MODEL and len(payload) < 0x77:
            raise ProtocolError("short DT inverter telemetry")
    telemetry = payload if kind == 0x04 and model != RELAY_ONLY_MODEL else None
    # HomeKit supplies line voltage even at zero load. Reject a blank/invalid sample.
    if telemetry is not None and family == 3:
        voltage_offsets = (0x39,) if model == "91000HKU" else (0x39, 0x3B, 0x3D)
        if not any(signed(payload, offset, 2) > 0 for offset in voltage_offsets):
            raise ProtocolError("missing HomeKit line voltage")
    sent_at = b"" if short_envelope else frame[payload_offset - 6 : payload_offset]
    return DeviceFrame(frame, family, kind, identity, model, serial, telemetry, sent_at)


def decode_device(frame: bytes) -> DeviceFrame | None:
    """Validate a complete framed device message and decode supported telemetry."""
    return decode_telemetry(parse_device(frame))


def _is_time_set(family: int, kind: int, payload: bytes, request: DeviceFrame) -> bool:
    """Match the documented clock payload: a valid date followed by six zero bytes."""
    types = (TIME_SET, (0x01, 0x03)) if request.family == 1 else (TIME_SET,)
    if (family, kind) not in types or payload[10:] != bytes(6):
        return False
    try:
        datetime(2000 + payload[4], *payload[5:10])
    except ValueError:
        return False
    return True


def classify_reply(frame: bytes, request: DeviceFrame) -> str | None:
    """Recognize genuine, identity-matched ACKs and time sync replies."""
    if frame == CLOUD_KEEPALIVE:
        return None
    _validate_crc(frame, "little")
    if len(frame) != 58 or frame[8:24] != request.identity:
        return None
    family, kind = frame[6:8]
    payload = _decrypt(frame[24:40], frame[40:-2])
    if request.kind == 0x03 and _is_time_set(family, kind, payload, request):
        return "time_sync"
    if (family, kind) == (request.family, request.kind):
        if payload == ACK:
            return "ack"
        if payload == NACK:
            return "nack"
    return None


def signed(payload: bytes, offset: int, length: int) -> int:
    """Read a signed, big-endian register after payload length validation."""
    return int.from_bytes(payload[offset : offset + length], signed=True)
