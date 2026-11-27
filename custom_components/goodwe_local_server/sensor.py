"""GoodWe telemetry and diagnostic sensor entities."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GoodWeConfigEntry
from .const import DOMAIN, Settings
from .protocol import METER_MODELS, RELAY_ONLY_MODEL, signed
from .server import DeviceState

SCAN_INTERVAL = timedelta(seconds=10)
# Allow small negative CT2 readings as possible standby draw or noise.
SOLAR_STANDBY_LIMIT_W = 100


@dataclass(frozen=True, kw_only=True)
class Reading(SensorEntityDescription):
    """Describe a telemetry value and its existing or new entity key."""

    read: Callable[[bytes, Settings], int | float | str | None]
    lifetime_offset: int | None = None


def _register(
    name: str,
    offset: int,
    length: int = 2,
    *,
    scale: float = 1,
    primary: bool = False,
    disabled: bool = False,
    device_class: SensorDeviceClass | None = None,
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT,
    unit: str | None = None,
    raw: bool = False,
) -> Reading:
    """Retain the original key, category and register interpretation."""
    key = re.sub("[^0-9a-z_]", "", name.lower().replace(" ", "_").replace("-", "_"))

    def read(payload: bytes, _settings: Settings):
        if len(payload) < offset + length:
            return None
        if raw:
            return payload[offset : offset + length].hex(" ")
        return scale * signed(payload, offset, length)

    return Reading(
        key=key,
        name=name,
        has_entity_name=True,
        entity_category=None if primary else EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=not disabled,
        device_class=device_class,
        state_class=state_class,
        native_unit_of_measurement=unit,
        read=read,
    )


energy = partial(
    _register,
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    unit="kWh",
    scale=0.01,
)
voltage = partial(_register, device_class=SensorDeviceClass.VOLTAGE, unit="V", scale=0.1)
current = partial(_register, device_class=SensorDeviceClass.CURRENT, unit="A", scale=0.1)
power = partial(_register, device_class=SensorDeviceClass.POWER, unit="W")
frequency = partial(_register, device_class=SensorDeviceClass.FREQUENCY, unit="Hz", scale=0.01)
temperature = partial(_register, device_class=SensorDeviceClass.TEMPERATURE, unit="°C", scale=0.1)
duration = partial(
    _register,
    device_class=SensorDeviceClass.DURATION,
    state_class=SensorStateClass.TOTAL_INCREASING,
    unit="h",
)
binary = partial(_register, disabled=True, state_class=None, raw=True)
integer = partial(_register, disabled=True)

# These tables retain the v0.1 register semantics, flag defaults and IDs.
METER_REGISTERS = (
    energy("L1 Energy Exported", 0x07, 4),
    energy("L2 Energy Exported", 0x0D, 4),
    energy("L3 Energy Exported", 0x13, 4),
    energy("Total Energy Exported", 0x19, 4, primary=True),
    energy("L1 Energy Imported", 0x1F, 4),
    energy("L2 Energy Imported", 0x25, 4),
    energy("L3 Energy Imported", 0x2B, 4),
    energy("Total Energy Imported", 0x31, 4, primary=True),
    voltage("L1 Voltage", 0x39),
    voltage("L2 Voltage", 0x3B),
    voltage("L3 Voltage", 0x3D),
    current("L1 Peak Current", 0x41, scale=0.01),
    current("L2 Peak Current", 0x45, scale=0.01),
    current("L3 Peak Current", 0x49, scale=0.01),
    power("L1 Export Power", 0x4B, 4),
    power("L2 Export Power", 0x4F, 4),
    power("L3 Export Power", 0x53, 4),
    power("Total Export Power", 0x57, 4, primary=True),
    binary("0x00-0x06", 0x00, 7),
    binary("0x0b-0x0c", 0x0B, 2),
    binary("0x11-0x12", 0x11, 2),
    binary("0x17-0x18", 0x17, 2),
    binary("0x1d-0x1e", 0x1D, 2),
    binary("0x23-0x24", 0x23, 2),
    binary("0x29-0x2a", 0x29, 2),
    binary("0x2f-0x30", 0x2F, 2),
    binary("0x35-0x36", 0x35, 2),
    binary("0x37-0x38", 0x37, 2),
    binary("0x5b-0x5e", 0x5B, 4),
    integer("0x5f-0x5f", 0x5F, 1),
    binary("0x60-0x6f", 0x60, 16),
)

DT_REGISTERS = (
    binary("0x00-0x14", 0x00, 0x15),
    binary("Timestamp", 0x15, 6),
    voltage("PV1 Voltage", 0x1B),
    current("PV1 Current", 0x1D),
    voltage("PV2 Voltage", 0x1F),
    current("PV2 Current", 0x21),
    binary("0x23-0x38", 0x23, 0x16),
    voltage("L1 Voltage", 0x39),
    voltage("L2 Voltage", 0x3B),
    voltage("L3 Voltage", 0x3D),
    current("L1 Current", 0x3F),
    current("L2 Current", 0x41),
    current("L3 Current", 0x43),
    frequency("L1 Frequency", 0x45),
    frequency("L2 Frequency", 0x47),
    frequency("L3 Frequency", 0x49),
    power("Total Power", 0x4B, 4, primary=True),
    binary("Work Mode", 0x4F),
    binary("0x51-0x56", 0x51, 6),
    binary("0x57-0x66", 0x57, 0x10),
    temperature("Temperature", 0x67),
    binary("0x69-0x6c", 0x69, 4),
    energy("Todays Energy", 0x6D, 2, scale=0.1, primary=True),
    energy("Total Energy", 0x6F, 4, scale=0.1, primary=True),
    duration("Runtime", 0x73, 4),
    binary("0x77-0x7e", 0x77, 6),
    binary("FunBit", 0x7D),
    voltage("Bus Voltage", 0x7F),
    voltage("Nbus Voltage", 0x81),
    binary("0x83-0x86", 0x83, 4),
    integer("0x87-0x88", 0x87, 2),
    binary("0x89-0x90", 0x89, 8),
    integer("0x91-0x92", 0x91),
    binary("0x93-0x96", 0x93, 4),
    current("PV1 Current again", 0x97, disabled=True),
    current("PV2 Current again", 0x99, disabled=True),
    binary("0x9b-0x9e", 0x9B, 4),
    binary("0x9f-0xa2", 0x9F, 4),
    temperature("0xa3-0xa4", 0xA3, disabled=True),
    binary("0xa5-0xa6", 0xA5),
    integer("0xa7-0xa8", 0xA7),
    binary("0xa9-0xaa", 0xA9),
    integer("0xab-0xac", 0xAB),
    binary("0xad-0xb0", 0xAD, 4),
    integer("0xb1-0xb2", 0xB1),
    binary("0xb3-0xcc", 0xB3, 0x1A),
    binary("0xcd-0x1ff", 0xCD, 0x43),
)


def _grid(payload: bytes) -> int:
    # New signed grid power uses positive import. The wire uses positive export.
    return -signed(payload, 0x4B, 4)


def _solar(payload: bytes, settings: Settings) -> int | None:
    solar = signed(payload, 0x4F, 4)
    if solar < -SOLAR_STANDBY_LIMIT_W:
        # A reversed or misplaced clamp must not look like a sunless day.
        return None
    return solar if solar > settings.solar_offset_w else 0


def _household(payload: bytes, settings: Settings) -> int | None:
    if (solar := _solar(payload, settings)) is None:
        return None
    watts = _grid(payload) + solar
    return watts if watts >= 0 else None


def _lifetime(offset: int, payload: bytes, _settings: Settings) -> float | None:
    value = signed(payload, offset, 4)
    return value * 0.01 if value >= 0 else None


def _hk1000_power(key: str, read: Callable) -> Reading:
    return Reading(
        key=key,
        translation_key=key,
        has_entity_name=True,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="W",
        read=read,
    )


def _hk1000_energy(key: str, offset: int) -> Reading:
    return Reading(
        key=key,
        translation_key=key,
        has_entity_name=True,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="kWh",
        read=partial(_lifetime, offset),
        lifetime_offset=offset,
    )


HK1000_READINGS = (
    _hk1000_power("signed_grid_power", lambda payload, _: _grid(payload)),
    _hk1000_power("grid_import_power", lambda payload, _: max(0, _grid(payload))),
    _hk1000_power("grid_export_power", lambda payload, _: max(0, -_grid(payload))),
    _hk1000_power("solar_generation_power", _solar),
    _hk1000_power("household_consumption_power", _household),
    _hk1000_energy("lifetime_grid_import_energy", 0x1F),
    _hk1000_energy("lifetime_grid_export_energy", 0x07),
    _hk1000_energy("lifetime_solar_generation_energy", 0x0D),
    Reading(
        key="grid_voltage",
        translation_key="grid_voltage",
        has_entity_name=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="V",
        read=lambda payload, _: signed(payload, 0x39, 2) * 0.1,
    ),
)


def _diagnostic(key: str, **kwargs) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=key,
        has_entity_name=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        **kwargs,
    )


DIAGNOSTICS = (
    _diagnostic("last_telemetry", device_class=SensorDeviceClass.TIMESTAMP),
    _diagnostic("last_connection", device_class=SensorDeviceClass.TIMESTAMP),
    _diagnostic(
        "data_age", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s"
    ),
    _diagnostic(
        "interval",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="s",
        suggested_display_precision=1,
    ),
    _diagnostic(
        "connection_health",
        device_class=SensorDeviceClass.ENUM,
        options=["waiting", "receiving", "stale"],
    ),
    _diagnostic(
        "cloud_health",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "disabled",
            "waiting",
            "healthy",
            "dns_failure",
            "blocked_address",
            "connect_failed",
            "reply_timeout",
            "upstream_disconnected",
            "invalid_reply",
            "cloud_rejected",
            "relay_error",
        ],
    ),
    _diagnostic("last_cloud_success", device_class=SensorDeviceClass.TIMESTAMP),
    _diagnostic("packet_counters"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoodWeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities when a known device first connects."""
    runtime = entry.runtime_data
    added: set[str] = set()

    @callback
    def add_device(device: DeviceState) -> None:
        if device.model == RELAY_ONLY_MODEL or device.serial in added:
            return
        added.add(device.serial)
        meter_model = METER_MODELS.get(device.model)
        info = DeviceInfo(
            identifiers={(DOMAIN, device.serial)},
            manufacturer="GoodWe",
            serial_number=device.serial,
            name="Energy Meter" if meter_model else "Solar Inverter",
            model="HomeKit" if meter_model else "DT Series",
            model_id=meter_model or "GW8KAU-DT",
        )
        readings = METER_REGISTERS if meter_model else DT_REGISTERS
        if meter_model == "HK1000":
            readings += HK1000_READINGS
        async_add_entities(
            [TelemetrySensor(device, info, desc, runtime.settings) for desc in readings]
            + [DiagnosticSensor(device, info, desc) for desc in DIAGNOSTICS]
        )

    runtime.on_device = add_device
    for device in runtime.devices.values():
        add_device(device)


class DeviceSensor(SensorEntity):
    """Share the exact v0.1 device and entity identities."""

    _attr_should_poll = False

    def __init__(
        self, device: DeviceState, info: DeviceInfo, desc: SensorEntityDescription
    ) -> None:
        self.device = device
        self.entity_description = desc
        self._attr_device_info = info
        self._attr_unique_id = f"{device.serial}_{desc.key}"


class TelemetrySensor(DeviceSensor):
    """Read the latest frame and update immediately on expiry."""

    entity_description: Reading

    def __init__(
        self, device: DeviceState, info: DeviceInfo, desc: Reading, settings: Settings
    ) -> None:
        super().__init__(device, info, desc)
        self.settings = settings

    async def async_added_to_hass(self) -> None:
        self.device.telemetry_listeners.add(self.async_write_ha_state)
        self.async_on_remove(
            partial(self.device.telemetry_listeners.discard, self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return self.device.fresh and self.native_value is not None

    @property
    def native_value(self) -> int | float | str | None:
        if (
            self.device.fresh
            and self.device.payload is not None
            and self.entity_description.lifetime_offset not in self.device.regressed_energy
        ):
            return self.entity_description.read(self.device.payload, self.settings)
        return None


class DiagnosticSensor(DeviceSensor):
    """Keep health and connection information visible during telemetry gaps."""

    async def async_added_to_hass(self) -> None:
        self.device.diagnostic_listeners.add(self.async_write_ha_state)
        self.async_on_remove(
            partial(self.device.diagnostic_listeners.discard, self.async_write_ha_state)
        )

    @property
    def should_poll(self) -> bool:
        return self.entity_description.key == "data_age"

    @property
    def native_value(self) -> int | float | str | datetime | None:
        if self.entity_description.key == "packet_counters":
            return self.device.counters["telemetry"]
        return getattr(self.device, self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        if self.entity_description.key == "packet_counters":
            return dict(self.device.counters)
        return None
