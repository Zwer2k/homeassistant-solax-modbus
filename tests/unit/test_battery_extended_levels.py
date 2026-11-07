"""Tests for the opt-in extended battery device levels."""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, call

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.solax_modbus import sensor
from custom_components.solax_modbus.config_flow import BATTERY_SCHEMA
from custom_components.solax_modbus.const import (
    CONF_READ_BATTERY,
    CONF_READ_BATTERY_EXTENDED,
    DOMAIN,
    INVERTER_IDENT,
    base_battery_config,
    battery_device_instance,
    battery_device_level,
)

HUB_NAME = "Sofar"


@dataclass(kw_only=True)
class FakeLevel(battery_device_level):
    selected: list[int] = field(default_factory=list)
    valid: bool = True

    async def select(self, hub: Any, instance: battery_device_instance) -> bool:
        self.selected.append(instance.number)
        return True

    async def verify(self, hub: Any, instance: battery_device_instance, new_data: dict[str, Any]) -> bool:
        return self.valid

    def get_device_details(self, new_data: dict[str, Any], key_prefix: str) -> dict[str, str]:
        return {"serial_number": new_data[key_prefix + "serial_number"], "sw_version": "V1"}


@dataclass
class FakeBatteryConfig(base_battery_config):
    levels: list[battery_device_level] = field(default_factory=list)
    pack_quantity: int = 0
    extended_pack_sensor_types: list[Any] = field(default_factory=list)
    pack_parent_key: str | None = None
    inverter_follow_ups: int = 0

    async def get_batt_pack_quantity(self, hub: Any) -> int:
        return self.pack_quantity

    async def select_battery(self, hub: Any, batt_nr: int, batt_pack_nr: int) -> bool:
        return True

    async def get_batt_pack_serial(self, hub: Any, batt_nr: int, batt_pack_nr: int) -> str:
        return "PACK-SN"

    def get_extended_pack_sensor_types(self, hub: Any) -> list[Any]:
        return self.extended_pack_sensor_types

    async def get_batt_quantity(self, hub: Any) -> int:
        return 1

    async def async_get_extended_levels(self, hub: Any) -> list[battery_device_level]:
        return self.levels

    def get_pack_parent_key(self, batt_nr: int) -> str | None:
        return self.pack_parent_key

    def get_extended_inverter_sensor_types(self, hub: Any) -> list[Any]:
        return ["inverter_extended"]

    async def async_inverter_follow_up(self, hub: Any, new_data: dict[str, Any]) -> None:
        self.inverter_follow_ups += 1


def _level() -> FakeLevel:
    return FakeLevel(
        name="Tower",
        sensor_types=[],
        instances=[
            battery_device_instance(key="battery_tower_1", name="Battery Tower 1", key_prefix="battery_tower_1_", number=1),
            battery_device_instance(
                key="battery_tower_1_unit_2",
                name="Battery Tower 1 Unit 2",
                key_prefix="battery_tower_1_unit_2_",
                number=2,
                parent_key="battery_tower_1",
            ),
        ],
    )


def _setup(monkeypatch: pytest.MonkeyPatch, config: base_battery_config) -> tuple[Mock, SimpleNamespace, Mock]:
    devices = {"battery_tower_1": SimpleNamespace(id="tower-1", serial_number="SN-1", sw_version=None)}

    def update_device(device_id: str, **changes: Any) -> None:
        device = next(device for device in devices.values() if device.id == device_id)
        for key, value in changes.items():
            setattr(device, key, value)

    registry = SimpleNamespace(
        async_update_device=Mock(side_effect=update_device),
        async_get_device_by_identifier=Mock(side_effect=lambda identifier, entry_id: devices.get(identifier[2])),
        async_get_device=Mock(return_value=None),
    )
    monkeypatch.setattr(dr, "async_get", lambda hass: registry)
    entity_to_list = Mock()
    monkeypatch.setattr(sensor, "entityToList", entity_to_list)
    monkeypatch.setattr(sensor, "entityToListSingle", Mock())
    link_parent = Mock()
    monkeypatch.setattr(sensor, "link_parent_device", link_parent)
    return entity_to_list, registry, link_parent


def _level_calls(entity_to_list: Mock) -> list[Any]:
    """Return the entityToList calls of battery levels; the inverter call uses no key prefix."""
    return [entity_call for entity_call in entity_to_list.call_args_list if entity_call.args[8]]


async def _setup_entry(config: base_battery_config, options: dict[str, Any]) -> None:
    hub = SimpleNamespace(
        name=HUB_NAME,
        device_info={},
        rebuild_blocks=Mock(),
        plugin=SimpleNamespace(BATTERY_CONFIG=config, SENSOR_TYPES=[], ENERGY_DASHBOARD_MAPPING=None, plugin_manufacturer="Sofar Solar"),
    )
    hass = cast(HomeAssistant, SimpleNamespace(data={DOMAIN: {HUB_NAME: {"hub": hub}}}))
    entry = cast(ConfigEntry, SimpleNamespace(data={}, options={"name": HUB_NAME, **options}, entry_id="entry"))
    assert await sensor.async_setup_entry(hass, entry, Mock())


def test_extended_option_defaults_to_disabled() -> None:
    """Existing installations keep their entities until the option is enabled."""
    assert BATTERY_SCHEMA({}) == {CONF_READ_BATTERY: False, CONF_READ_BATTERY_EXTENDED: False}


async def test_base_battery_config_has_no_extended_levels() -> None:
    """Plugins without extended levels are unaffected by the option."""
    assert await base_battery_config().async_get_extended_levels(None) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        pytest.param({CONF_READ_BATTERY: True}, id="extended-disabled"),
        pytest.param({CONF_READ_BATTERY: False, CONF_READ_BATTERY_EXTENDED: True}, id="battery-readout-disabled"),
    ],
)
async def test_extended_levels_require_both_options(monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]) -> None:
    """No extra devices are created without the explicit opt-in."""
    config = FakeBatteryConfig(levels=[_level()])
    entity_to_list, _, _ = _setup(monkeypatch, config)

    await _setup_entry(config, options)

    assert _level_calls(entity_to_list) == []


@pytest.mark.asyncio
async def test_extended_levels_create_one_device_per_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each level instance gets its own device, key prefix and selection callback."""
    level = _level()
    config = FakeBatteryConfig(levels=[level])
    entity_to_list, _, link_parent = _setup(monkeypatch, config)

    await _setup_entry(config, {CONF_READ_BATTERY: True, CONF_READ_BATTERY_EXTENDED: True})

    level_calls = _level_calls(entity_to_list)
    device_infos = [level_call.args[5] for level_call in level_calls]
    assert [info["identifiers"] for info in device_infos] == [
        {(DOMAIN, HUB_NAME, "battery_tower_1")},
        {(DOMAIN, HUB_NAME, "battery_tower_1_unit_2")},
    ]
    assert [info["name"] for info in device_infos] == ["Sofar Battery Tower 1", "Sofar Battery Tower 1 Unit 2"]
    assert [level_call.args[8] for level_call in level_calls] == ["battery_tower_1_", "battery_tower_1_unit_2_"]
    assert [link_call.args[2] for link_call in link_parent.call_args_list] == [
        (DOMAIN, HUB_NAME, INVERTER_IDENT),
        (DOMAIN, HUB_NAME, "battery_tower_1"),
    ]

    read_preparation = level_calls[1].args[9]
    assert await read_preparation({}) is True
    assert level.selected == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("valid", "expected_updates"),
    [
        pytest.param(True, [call("tower-1", sw_version="V1")], id="verified"),
        pytest.param(False, [], id="rejected"),
    ],
)
async def test_extended_level_follow_up_updates_changed_device_details(
    monkeypatch: pytest.MonkeyPatch, valid: bool, expected_updates: list[Any]
) -> None:
    """Device details are written only for verified data and only when they changed."""
    level = _level()
    level.valid = valid
    config = FakeBatteryConfig(levels=[level])
    entity_to_list, registry, _ = _setup(monkeypatch, config)

    await _setup_entry(config, {CONF_READ_BATTERY: True, CONF_READ_BATTERY_EXTENDED: True})
    read_follow_up = _level_calls(entity_to_list)[0].args[10]

    for _ in range(2):
        assert await read_follow_up({}, {"battery_tower_1_serial_number": "SN-1"}) is valid

    assert registry.async_update_device.call_args_list == expected_updates


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extended", "expected_sensors"),
    [
        pytest.param(False, ["base"], id="extended-disabled"),
        pytest.param(True, ["base", "extended"], id="extended-enabled"),
    ],
)
async def test_extended_pack_sensors_require_option(monkeypatch: pytest.MonkeyPatch, extended: bool, expected_sensors: list[str]) -> None:
    """Additional pack sensors join the existing pack device only with the opt-in."""
    config = FakeBatteryConfig(
        battery_sensor_type=cast(Any, ["base"]),
        battery_sensor_key_prefix="battery_{batt-nr}_{pack-nr}_",
        pack_quantity=1,
        extended_pack_sensor_types=["extended"],
    )
    entity_to_list, _, _ = _setup(monkeypatch, config)

    await _setup_entry(config, {CONF_READ_BATTERY: True, CONF_READ_BATTERY_EXTENDED: extended})

    pack_call = next(entity_call for entity_call in entity_to_list.call_args_list if entity_call.args[8] == "battery_1_1_")
    assert pack_call.args[6] == expected_sensors


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extended", "expected_parent"),
    [
        pytest.param(False, INVERTER_IDENT, id="extended-disabled"),
        pytest.param(True, "battery_1", id="extended-enabled"),
    ],
)
async def test_pack_parent_from_battery_config(monkeypatch: pytest.MonkeyPatch, extended: bool, expected_parent: str) -> None:
    """Packs move below their tower device only with the opt-in."""
    config = FakeBatteryConfig(battery_sensor_key_prefix="battery_{batt-nr}_{pack-nr}_", pack_quantity=1, pack_parent_key="battery_1")
    _, _, link_parent = _setup(monkeypatch, config)

    await _setup_entry(config, {CONF_READ_BATTERY: True, CONF_READ_BATTERY_EXTENDED: extended})

    assert [link_call.args[2] for link_call in link_parent.call_args_list] == [(DOMAIN, HUB_NAME, expected_parent)]


@pytest.mark.asyncio
@pytest.mark.parametrize("extended", [False, True], ids=["extended-disabled", "extended-enabled"])
async def test_base_battery_config_creates_no_devices(monkeypatch: pytest.MonkeyPatch, extended: bool) -> None:
    """A plugin that sets BATTERY_CONFIG without overriding the pack methods gets no battery devices."""
    config = base_battery_config(battery_sensor_key_prefix="battery_{batt-nr}_{pack-nr}_")
    entity_to_list, _, link_parent = _setup(monkeypatch, config)

    await _setup_entry(config, {CONF_READ_BATTERY: True, CONF_READ_BATTERY_EXTENDED: extended})

    assert _level_calls(entity_to_list) == []
    link_parent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extended", "expected_sensors", "expected_follow_ups"),
    [
        pytest.param(False, [], 0, id="extended-disabled"),
        pytest.param(True, ["inverter_extended"], 1, id="extended-enabled"),
    ],
)
async def test_extended_inverter_hooks_require_option(
    monkeypatch: pytest.MonkeyPatch, extended: bool, expected_sensors: list[str], expected_follow_ups: int
) -> None:
    """Inverter sensors and the follow-up of the extended readout only run with the opt-in."""
    config = FakeBatteryConfig()
    entity_to_list, _, _ = _setup(monkeypatch, config)

    await _setup_entry(config, {CONF_READ_BATTERY: True, CONF_READ_BATTERY_EXTENDED: extended})

    inverter_call = next(entity_call for entity_call in entity_to_list.call_args_list if entity_call.args[8] == "")
    assert inverter_call.args[6] == expected_sensors
    assert await inverter_call.args[10]({}, {}) is True
    assert config.inverter_follow_ups == expected_follow_ups
