"""Independent synthetic frame builders and socket test helpers."""

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.goodwe_local_server.protocol import crc16

FIXTURES = Path(__file__).parent / "fixtures"
IV = bytes(range(16))


def capture(name):
    return bytes.fromhex((FIXTURES / f"{name}.hex").read_text())


def encrypt(payload, iv=IV):
    encryptor = Cipher(algorithms.AES(bytes([255]) * 16), modes.CBC(iv)).encryptor()
    return encryptor.update(payload) + encryptor.finalize()


def envelope(prefix, body, byteorder):
    frame = prefix + (len(body) - 1).to_bytes(4) + body
    return frame + crc16(frame).to_bytes(2, byteorder)


def stamp(hour=0, minute=0, second=0):
    """Build a device envelope timestamp (Y M D H m s)."""
    return bytes([24, 1, 2, hour, minute, second])


def device_packet(model=b"91000HKU", serial=b"TEST0001", *, kind=4, payload=None, sent=None, iv=IV):
    family = 3 if model.endswith(b"HKU") else 1
    if payload is None:
        payload = meter_payload()
    body = bytes([family, kind])
    if kind == 0x10:
        body += model + serial + iv
    else:
        body += bytes(2) + model + serial + iv + (stamp() if sent is None else sent)
    return envelope(b"POSTGW", body + encrypt(payload, iv), "big")


def captured_body(name, family, kind):
    """Frame a published decrypted-envelope body whose header was not published."""
    return envelope(b"POSTGW", bytes([family, kind]) + capture(name), "big")


def time_set_packet(model, serial=b"TEST0001"):
    """Build the documented (0x01, 0x16) SEMS time-set reply."""
    payload = bytes.fromhex("12161218") + stamp(5, 19, 14) + bytes(6)
    body = bytes([1, 0x16]) + model + serial + IV + encrypt(payload)
    return envelope(b"GW", body, "little")


def reply_packet(model=b"91000HKU", serial=b"TEST0001", *, kind=4, payload=bytes(16)):
    family = 3 if model.endswith(b"HKU") else 1
    body = bytes([family, kind]) + model + serial + IV + encrypt(payload)
    return envelope(b"GW", body, "little")


def meter_payload(grid=-150, solar=0, *, voltage=2372, import_energy=32000):
    payload = bytearray(0x70)
    for offset, size, value in (
        (0x07, 4, 4999),
        (0x0D, 4, 54157),
        (0x19, 4, 59156),
        (0x1F, 4, import_energy),
        (0x31, 4, 999999),
        (0x39, 2, voltage),
        (0x41, 2, 84),
        (0x45, 2, 27),
        (0x4B, 4, grid),
        (0x4F, 4, solar),
        (0x57, 4, grid + solar),
    ):
        payload[offset : offset + size] = value.to_bytes(size, signed=True)
    return bytes(payload)


async def until(condition, timeout=2):
    async with asyncio.timeout(timeout):
        while not (result := condition()):
            await asyncio.sleep(0.002)
        return result


@asynccontextmanager
async def tcp_client(port):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        yield reader, writer
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
