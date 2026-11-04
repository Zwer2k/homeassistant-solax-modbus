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
    base_battery_config,
    battery_device_level,
)

HUB_NAME = "Sofar"


@dataclass(kw_only=True)
class FakeLevel(battery_device_level):
    quantity: int = 2
    selected: list[int] = field(default_factory=list)
    valid: bool = True

    async def get_quantity(self, hub: Any) -> int:
        return self.quantity

    async def select(self, hub: Any, index: int) -> bool:
        self.selected.append(index)
        return True

    async def verify(self, hub: Any, index: int, new_data: dict[str, Any], key_prefix: str) -> bool:
        return self.valid

    def get_device_details(self, new_data: dict[str, Any], key_prefix: str) -> dict[str, str]:
        return {"serial_number": new_data[key_prefix + "serial_number"], "sw_version": "V1"}


@dataclass
class FakeBatteryConfig(base_battery_config):
    levels: list[battery_device_level] = field(default_factory=list)

    async def get_batt_pack_quantity(self, hub: Any) -> int:
        return 0

    async def get_batt_quantity(self, hub: Any) -> int:
        return 1

    async def async_get_extended_levels(self, hub: Any) -> list[battery_device_level]:
        return self.levels


def _level() -> FakeLevel:
    return FakeLevel(key="battery_tower_{nr}", name="Battery Tower {nr}", sensor_key_prefix="battery_tower_{nr}_", sensor_types=[])


def _setup(monkeypatch: pytest.MonkeyPatch, config: base_battery_config) -> tuple[Mock, SimpleNamespace, dict[str, SimpleNamespace]]:
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
    return entity_to_list, registry, devices


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
    entity_to_list, _, _ = _setup(monkeypatch, config)

    await _setup_entry(config, {CONF_READ_BATTERY: True, CONF_READ_BATTERY_EXTENDED: True})

    level_calls = _level_calls(entity_to_list)
    device_infos = [level_call.args[5] for level_call in level_calls]
    assert [info["identifiers"] for info in device_infos] == [
        {(DOMAIN, HUB_NAME, "battery_tower_1")},
        {(DOMAIN, HUB_NAME, "battery_tower_2")},
    ]
    assert [info["name"] for info in device_infos] == ["Sofar Battery Tower 1", "Sofar Battery Tower 2"]
    assert [level_call.args[8] for level_call in level_calls] == ["battery_tower_1_", "battery_tower_2_"]

    read_preparation = level_calls[1].args[9]
    assert await read_preparation({}) is True
    assert level.selected == [1]


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
