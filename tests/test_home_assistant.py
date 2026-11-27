"""Run real Home Assistant config entry, entity registry and lifecycle flows."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from voluptuous import Invalid

from custom_components.goodwe_local_server import GoodWeServer, entry_settings
from custom_components.goodwe_local_server.const import DOMAIN, Settings
from custom_components.goodwe_local_server.diagnostics import async_get_config_entry_diagnostics

from .helpers import device_packet, meter_payload, tcp_client, until

REPO = Path(__file__).resolve().parents[1]
METER_SERIAL = "TEST0001"
LEGACY_ENTITY_ID = "sensor.house_meter_existing_export_power"


@asynccontextmanager
async def home_assistant(path):
    if not (components := path / "custom_components").exists():
        components.symlink_to(REPO / "custom_components", target_is_directory=True)
    hass = HomeAssistant(str(path))
    hass.config.skip_pip = True
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await loader.async_get_custom_components(hass)
    assert await bootstrap.async_load_base_functionality(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_start()
    try:
        yield hass
    finally:
        if hass.state is not CoreState.not_running:
            await hass.async_stop()


@pytest.fixture(autouse=True)
def ephemeral_listener(monkeypatch):
    """Bind test listeners to OS-assigned ports unless one is explicitly selected."""
    original = GoodWeServer.start

    async def start(runtime):
        settings = runtime.settings
        if settings.listen_port == Settings().listen_port:
            runtime.settings = replace(settings, listen_port=0)
        try:
            await original(runtime)
        finally:
            runtime.settings = settings

    monkeypatch.setattr(GoodWeServer, "start", start)


def _port(runtime):
    return runtime.server.sockets[0].getsockname()[1]


def _state(hass, entity_id):
    if entity_id is not None and (state := hass.states.get(entity_id)) is not None:
        return state.state
    return None


def _id(hass, key, serial=METER_SERIAL):
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{serial}_{key}")


async def _setup_entry(hass):
    flow_manager = hass.config_entries.flow
    flow = await flow_manager.async_init(DOMAIN, context={"source": "user"})
    assert flow["type"] is FlowResultType.FORM
    added = await flow_manager.async_configure(flow["flow_id"], {})
    assert added["type"] is FlowResultType.CREATE_ENTRY
    entry = added["result"]
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_config_options_validation_and_reload(tmp_path):
    async with home_assistant(tmp_path) as hass:
        entry = await _setup_entry(hass)
        assert entry_settings(entry) == Settings()
        duplicate = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert duplicate["reason"] == "single_instance_allowed"
        manager = hass.config_entries.options
        flow = await manager.async_init(entry.entry_id)
        assert flow["type"] is FlowResultType.FORM
        flow_id = flow["flow_id"]
        with pytest.raises(Invalid):
            await manager.async_configure(flow_id, {"stale_after": 0})
        with pytest.raises(Invalid):
            await manager.async_configure(flow_id, {"listen_port": 65536})
        with pytest.raises(Invalid):
            await manager.async_configure(flow_id, {"solar_offset_w": 51})
        invalid = await manager.async_configure(
            flow_id, {"upstream_host": "https://example.com", "upstream_address": "::zzz"}
        )
        assert invalid["errors"] == {
            "upstream_host": "invalid_hostname",
            "upstream_address": "invalid_address",
        }
        invalid = await manager.async_configure(
            flow_id, {"upstream_host": "example.com", "upstream_address": "127.0.0.1"}
        )
        assert invalid["errors"] == {"upstream_address": "non_public_address"}
        previous = entry.runtime_data
        async with tcp_client(_port(previous)) as (reader, _writer):
            saved = await manager.async_configure(
                flow_id,
                {
                    "stale_after": 120,
                    "solar_offset_w": 7,
                    "forward_to_cloud": True,
                    "upstream_host": "EXAMPLE.COM.",
                    "upstream_address": "8.8.4.4",
                },
            )
            assert saved["type"] is FlowResultType.CREATE_ENTRY
            await hass.async_block_till_done()
            assert await asyncio.wait_for(reader.read(), 1) == b""
        assert entry.runtime_data is not previous
        assert previous.server is None
        assert entry_settings(entry).stale_after == 120
        assert entry.runtime_data.endpoint is not None
        assert entry_settings(entry).solar_offset_w == 7
        assert entry_settings(entry).upstream_host == "example.com"
        assert entry_settings(entry).upstream_address == "8.8.4.4"


async def test_home_assistant_entities_recover_after_stale_and_unload(tmp_path):
    async with home_assistant(tmp_path) as hass:
        entry = await _setup_entry(hass)
        # Exercise the same scheduled expiry that runs in production with a short test delay.
        runtime = entry.runtime_data
        runtime.settings = Settings(stale_after=0.15)
        async with tcp_client(_port(runtime)) as (reader, writer):
            writer.write(device_packet(payload=meter_payload(-450, 5)))
            await until(lambda: _state(hass, _id(hass, "grid_import_power")) == "450")
            entity_id = _id(hass, "grid_import_power")
            assert _state(hass, _id(hass, "total_export_power")) == "-445"
            assert _state(hass, _id(hass, "connection_health")) == "receiving"
            assert _state(hass, _id(hass, "cloud_health")) == "disabled"
            await until(lambda: _state(hass, entity_id) == "unavailable")
            assert _state(hass, _id(hass, "connection_health")) == "stale"
            assert _state(hass, _id(hass, "data_age")) is not None
            writer.write(device_packet(payload=meter_payload(300, 1200)))
            await until(lambda: _state(hass, entity_id) == "0")
            assert _state(hass, _id(hass, "grid_export_power")) == "300"
            assert _state(hass, _id(hass, "household_consumption_power")) == "900"
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert await asyncio.wait_for(reader.read(), 1) == b""
        assert runtime.server is None
        assert runtime.devices[METER_SERIAL].expiry is None
        assert not runtime.devices[METER_SERIAL].telemetry_listeners
        assert not runtime.devices[METER_SERIAL].diagnostic_listeners


async def test_legacy_entry_settings_identity_and_restart(tmp_path):
    storage = tmp_path / ".storage"
    storage.mkdir()
    entry_id = "01LEGACYGOODWE000000000000"
    (storage / "core.config_entries").write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 5,
                "key": "core.config_entries",
                "data": {
                    "entries": [
                        {
                            "entry_id": entry_id,
                            "version": 1,
                            "minor_version": 1,
                            "domain": DOMAIN,
                            "title": "GoodWe Local Server",
                            "data": {},
                            "options": {},
                            "source": "user",
                            "unique_id": None,
                            "disabled_by": None,
                            "pref_disable_new_entities": False,
                            "pref_disable_polling": False,
                            "created_at": "2025-01-01T00:00:00+00:00",
                            "modified_at": "2025-01-01T00:00:00+00:00",
                            "discovery_keys": {},
                            "subentries": [],
                        }
                    ],
                },
            }
        )
    )

    async with home_assistant(tmp_path) as hass:
        entry = hass.config_entries.async_get_known_entry(entry_id)
        assert entry_settings(entry) == Settings()
        legacy_device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry_id, identifiers={(DOMAIN, METER_SERIAL)}, name="Energy Meter"
        )
        registry = er.async_get(hass)
        existing = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{METER_SERIAL}_total_export_power",
            suggested_object_id="house_meter_existing_export_power",
            config_entry=entry,
            device_id=legacy_device.id,
        )
        assert existing.entity_id == LEGACY_ENTITY_ID
        assert entry.state is ConfigEntryState.LOADED
        first_runtime = entry.runtime_data
        async with tcp_client(_port(first_runtime)) as (_reader, writer):
            writer.write(device_packet(payload=meter_payload(-700, 1200)))
            await until(lambda: _state(hass, LEGACY_ENTITY_ID) == "500")
        assert registry.async_get(LEGACY_ENTITY_ID).device_id == legacy_device.id
        assert _id(hass, "total_export_power") == LEGACY_ENTITY_ID
        assert registry.async_get(_id(hass, "0x00_0x06")).disabled
        assert _id(hass, "lifetime_grid_import_energy") != _id(hass, "total_energy_imported")

    assert first_runtime.server is None
    async with home_assistant(tmp_path) as hass:
        entry = hass.config_entries.async_get_known_entry(entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert not entry.runtime_data.devices
        assert _state(hass, LEGACY_ENTITY_ID) == "unavailable"
        async with tcp_client(_port(entry.runtime_data)) as (_reader, writer):
            writer.write(device_packet(payload=meter_payload(-200, 1100)))
            await until(lambda: _state(hass, LEGACY_ENTITY_ID) == "900")
        assert er.async_get(hass).async_get(LEGACY_ENTITY_ID).device_id == legacy_device.id


async def test_diagnostics_privacy_and_legacy_models(tmp_path):
    async with home_assistant(tmp_path) as hass:
        entry = await _setup_entry(hass)
        runtime = entry.runtime_data
        runtime.settings = Settings(upstream_host="private.example", upstream_address="8.8.4.4")
        async with (
            tcp_client(_port(runtime)) as (_reader, meter),
            tcp_client(_port(runtime)) as (_reader, inverter),
        ):
            meter.write(device_packet(b"93000HKU", b"METER003"))
            inverter.write(device_packet(b"58000DTA", b"INV00001", payload=bytes(0x110)))
            await until(lambda: _id(hass, "total_power", "INV00001"))
            await until(lambda: _id(hass, "l1_voltage", "METER003"))
        assert _id(hass, "grid_import_power", "METER003") is None
        assert _state(hass, _id(hass, "total_power", "INV00001")) == "0"
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        exported = json.dumps(diagnostics)
        assert "METER003" not in exported and "INV00001" not in exported
        assert "private.example" not in exported and "8.8.4.4" not in exported
        assert "payload" not in exported
        assert sorted(device["model"] for device in diagnostics["devices"]) == [
            "58000DTA",
            "93000HKU",
        ]
        assert diagnostics["counters"]["telemetry"] == 2


async def test_busy_port_recovery_releases_earlier_listener(tmp_path):
    def reject(_reader, writer):
        writer.close()

    async with home_assistant(tmp_path) as hass:
        entry = await _setup_entry(hass)
        previous = entry.runtime_data
        async with await asyncio.start_server(reject, "0.0.0.0", 0) as occupied:
            port = occupied.sockets[0].getsockname()[1]
            hass.config_entries.async_update_entry(entry, options={"listen_port": port})
            assert not await hass.config_entries.async_reload(entry.entry_id)
            assert entry.state is ConfigEntryState.SETUP_RETRY
            assert previous.server is None
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert _port(entry.runtime_data) == port
