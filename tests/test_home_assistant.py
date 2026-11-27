"""Run real Home Assistant config entry, entity registry and lifecycle flows."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from shutil import copytree, ignore_patterns

import probatio
import pytest
from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from voluptuous import Invalid

from custom_components.goodwe_local_server import GoodWeServer, entry_settings
from custom_components.goodwe_local_server.const import DOMAIN, Settings
from custom_components.goodwe_local_server.diagnostics import async_get_config_entry_diagnostics

from .helpers import device_packet, meter_payload, stamp, tcp_client, until

REPO = Path(__file__).resolve().parents[1]
METER_SERIAL = "TEST0001"
LEGACY_ENTITY_ID = "sensor.house_meter_existing_export_power"


@asynccontextmanager
async def home_assistant(path):
    if not (components := path / "custom_components").exists():
        copytree(REPO / "custom_components", components, ignore=ignore_patterns("__pycache__"))
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
            writer.write(device_packet(payload=meter_payload(300, 1200), sent=stamp(second=1)))
            await until(lambda: _state(hass, entity_id) == "0")
            assert _state(hass, _id(hass, "grid_export_power")) == "300"
            assert _state(hass, _id(hass, "household_consumption_power")) == "900"
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert await asyncio.wait_for(reader.read(), 1) == b""
        assert runtime.server is None
        assert runtime.devices[METER_SERIAL].expiry is None
        assert not runtime.devices[METER_SERIAL].telemetry_listeners
        assert not runtime.devices[METER_SERIAL].diagnostic_listeners


async def test_hk1000_lifetime_counter_regression_does_not_become_a_recorder_reset(tmp_path):
    async with home_assistant(tmp_path) as hass:
        entry = await _setup_entry(hass)
        runtime = entry.runtime_data
        async with tcp_client(_port(runtime)) as (_, writer):
            writer.write(device_packet(payload=meter_payload(-400)))
            key = "lifetime_grid_import_energy"
            await until(lambda: _state(hass, _id(hass, key)) == "320.0")
            assert hass.states.get(_id(hass, key)).attributes["state_class"] == "total_increasing"
            writer.write(
                device_packet(payload=meter_payload(-500, import_energy=0), sent=stamp(second=1))
            )
            await until(lambda: _state(hass, _id(hass, key)) == "unavailable")
            assert _state(hass, _id(hass, "grid_import_power")) == "500"
            assert _state(hass, _id(hass, "l1_energy_imported")) == "0.0"
            writer.write(
                device_packet(
                    payload=meter_payload(-500, import_energy=32001), sent=stamp(second=2)
                )
            )
            await until(lambda: _state(hass, _id(hass, key)) == "320.01")
            assert runtime.devices[METER_SERIAL].counters["invalid_lifetime_energy"] == 1


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


async def test_options_form_serializes_and_ip_override_can_be_cleared(tmp_path):
    async with home_assistant(tmp_path) as hass:
        entry = await _setup_entry(hass)
        manager = hass.config_entries.options
        flow = await manager.async_init(entry.entry_id)
        await manager.async_configure(flow["flow_id"], {"upstream_address": "8.8.4.4"})
        await hass.async_block_till_done()
        assert entry_settings(entry).upstream_address == "8.8.4.4"

        flow = await manager.async_init(entry.entry_id)
        # The frontend receives this serialized form and omits a cleared optional field.
        fields = probatio.to_field_list(flow["data_schema"], custom_serializer=cv.custom_serializer)
        saved = await manager.async_configure(flow["flow_id"], {"stale_after": 200})
        assert saved["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert entry_settings(entry).upstream_address == ""
        assert entry_settings(entry).stale_after == 200
        override = next(field for field in fields if field["name"] == "upstream_address")
        assert override["description"] == {"suggested_value": "8.8.4.4"}
        assert "default" not in override


async def test_reversed_inverter_ct_is_unavailable_in_home_assistant(tmp_path):
    async with home_assistant(tmp_path) as hass:
        entry = await _setup_entry(hass)
        async with tcp_client(_port(entry.runtime_data)) as (_reader, writer):
            writer.write(device_packet(payload=meter_payload(-3630, -3898)))
            await until(lambda: _state(hass, _id(hass, "grid_import_power")) == "3630")
            assert _state(hass, _id(hass, "solar_generation_power")) == "unavailable"
            assert _state(hass, _id(hass, "household_consumption_power")) == "unavailable"
            assert _state(hass, _id(hass, "l2_export_power")) == "-3898"


async def test_diagnostics_and_clean_shutdown_when_the_port_is_busy(tmp_path, caplog):
    def reject(_reader, writer):
        writer.close()

    async with home_assistant(tmp_path) as hass:
        entry = await _setup_entry(hass)
        async with await asyncio.start_server(reject, "0.0.0.0", 0) as occupied:
            port = occupied.sockets[0].getsockname()[1]
            hass.config_entries.async_update_entry(entry, options={"listen_port": port})
            assert not await hass.config_entries.async_reload(entry.entry_id)
            diagnostics = await async_get_config_entry_diagnostics(hass, entry)
            assert diagnostics["listener_running"] is False
        assert await hass.config_entries.async_reload(entry.entry_id)
    assert "Unable to remove unknown job listener" not in caplog.text


async def test_removing_the_integration_releases_the_port(tmp_path):
    async with home_assistant(tmp_path) as hass:
        entry = await _setup_entry(hass)
        runtime = entry.runtime_data
        port = _port(runtime)
        async with tcp_client(port) as (reader, writer):
            writer.write(device_packet())
            await until(lambda: _state(hass, _id(hass, "grid_import_power")) == "150")
            await hass.config_entries.async_remove(entry.entry_id)
            await hass.async_block_till_done()
            assert await asyncio.wait_for(reader.read(), 1) == b""
        assert runtime.server is None
        assert _id(hass, "grid_import_power") is None
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
        await _setup_entry(hass)


async def test_failed_platform_setup_releases_the_listener(tmp_path, monkeypatch):
    async with home_assistant(tmp_path) as hass:
        entry = await _setup_entry(hass)
        assert await hass.config_entries.async_unload(entry.entry_id)

        async def fail(*_args):
            raise RuntimeError("platform failure")

        with monkeypatch.context() as patch:
            patch.setattr(hass.config_entries, "async_forward_entry_setups", fail)
            assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_failed_unload_keeps_receiving(tmp_path, monkeypatch):
    async with home_assistant(tmp_path) as hass:
        entry = await _setup_entry(hass)
        runtime = entry.runtime_data
        async with tcp_client(_port(runtime)) as (_reader, writer):
            writer.write(device_packet())
            await until(lambda: _state(hass, _id(hass, "grid_import_power")) == "150")

            async def refuse(*_args):
                return False

            with monkeypatch.context() as patch:
                patch.setattr(hass.config_entries, "async_unload_platforms", refuse)
                assert not await hass.config_entries.async_unload(entry.entry_id)
            assert runtime.server is not None and runtime.on_device is not None
            writer.write(device_packet(payload=meter_payload(-275), sent=stamp(second=1)))
            await until(lambda: _state(hass, _id(hass, "grid_import_power")) == "275")
