"""Compare every v0.1 entity with values recorded by running the original code.

fixtures/v01_entities.json was produced by executing the unmodified sensor.py
from fork baseline 42880e2 against a deterministic payload.
"""

import json

import pytest

from custom_components.goodwe_local_server.const import DOMAIN, Settings
from custom_components.goodwe_local_server.protocol import METER_MODELS, decode_device
from custom_components.goodwe_local_server.sensor import (
    DT_REGISTERS,
    HK1000_READINGS,
    METER_REGISTERS,
)

from .helpers import FIXTURES, device_packet

GOLDEN = json.loads((FIXTURES / "v01_entities.json").read_text())
PAYLOAD = bytes.fromhex(GOLDEN["payload"])
SERIAL = "TEST0001"


def test_golden_table_covers_all_legacy_registers():
    assert {model: len(rows) for model, rows in GOLDEN["models"].items()} == {
        "93000HKU": 31,
        "91000HKU": 31,
        "58000DTA": 47,
    }
    assert len(METER_REGISTERS) + len(DT_REGISTERS) == 78


@pytest.mark.parametrize("model", GOLDEN["models"])
def test_legacy_identity_metadata_and_values_match_v01(model):
    registers = METER_REGISTERS if model in METER_MODELS else DT_REGISTERS
    payload = PAYLOAD[:0x70] if model in METER_MODELS else PAYLOAD
    telemetry = decode_device(device_packet(model.encode(), payload=payload)).telemetry
    expected = GOLDEN["models"][model]
    assert [desc.key for desc in registers] == [row["key"] for row in expected]
    for desc, row in zip(registers, expected, strict=True):
        assert row["unique_id"] == f"{SERIAL}_{desc.key}"
        assert row["device"]["identifiers"] == [[DOMAIN, SERIAL]]
        actual = {
            "name": desc.name,
            "has_entity_name": desc.has_entity_name,
            "device_class": desc.device_class,
            "state_class": desc.state_class,
            "unit": desc.native_unit_of_measurement,
            "entity_category": desc.entity_category,
            "enabled_default": desc.entity_registry_enabled_default,
        }
        assert actual == {key: row[key] for key in actual}, desc.key
        value = desc.read(telemetry, Settings())
        # The payload covers every register, so no value may differ or vanish.
        assert value == row["value"], desc.key
        assert type(value) is type(row["value"]), desc.key


def test_new_hk1000_keys_do_not_collide_with_legacy_keys():
    assert not {d.key for d in HK1000_READINGS} & {d.key for d in METER_REGISTERS}
