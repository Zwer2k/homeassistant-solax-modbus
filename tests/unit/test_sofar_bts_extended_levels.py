"""Tests for the SOFAR BTS BDU and PCU device levels."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.solax_modbus.const import battery_device_instance
from custom_components.solax_modbus.plugin_sofar import (
    BAT_BTS,
    BATTERY_EXTENDED_SENSOR_TYPES,
    BDU_FAULT_TEXTS,
    HYBRID,
    PCU_FAULT_TEXTS,
    PCU_WARNING_TEXTS,
    X3,
    battery_config,
    sofar_selectable_level,
    value_function_bdu_version,
    value_function_bit_count,
    value_function_bit_texts,
    value_function_hardware_version,
    value_function_pcu_version,
)

BDU_SERIAL = [0x5348, 0x3230, 0x3035, 0x3035, 0x304B, 0x4532, 0x3238, 0x3136, 0x3432, 0x3639]


class FakeHub:
    """Inverter whose query registers select the device reported in the ID register that follows."""

    def __init__(self, registers: dict[int, int], devices: dict[int, list[int]] | None = None, follow_selection: bool = True) -> None:
        self.name = "Sofar"
        self._modbus_addr = 1
        self._invertertype = HYBRID | X3 | BAT_BTS
        self.registers = registers
        self.devices = devices or {}
        self.follow_selection = follow_selection
        self.writes: list[tuple[int, int]] = []

    async def async_read_holding_registers(self, unit: int, address: int, count: int) -> SimpleNamespace:
        return SimpleNamespace(registers=[self.registers.get(address + i, 0) for i in range(count)], isError=lambda: False)

    async def async_write_registers_single(self, unit: int, address: int, payload: int) -> None:
        self.writes.append((address, payload))
        self.registers[address] = payload
        if self.follow_selection and address == 0x60C4:
            self.registers[0x6090] = payload
            for i, value in enumerate(self.devices.get(payload, [0] * 10)):
                self.registers[0x6091 + i] = value


async def _discover(hub: Any) -> tuple[battery_config, dict[str, list[battery_device_instance]]]:
    config = battery_config()
    levels = await config.async_get_extended_levels(hub)
    return config, {level.name: level.instances for level in levels}


def _bdu_level() -> sofar_selectable_level:
    return sofar_selectable_level(name="BDU", sensor_types=[], inquire_address=0x60C4, id_address=0x6090)


def _instance(number: int, key_prefix: str = "battery_1_bdu_") -> battery_device_instance:
    return battery_device_instance(key=f"battery_{number}", name=f"Battery {number}", key_prefix=key_prefix, number=number)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", sleep)


def test_pcu_version_decodes_binary_digits() -> None:
    assert value_function_pcu_version([0x5601, 0x0021], None, {}) == "V01.00.33"


def test_bdu_version_decodes_ascii_digit_pairs() -> None:
    assert value_function_bdu_version([0x0056, 0x3031, 0x3030, 0x3130], None, {}) == "V01.00.10"


def test_hardware_version() -> None:
    assert value_function_hardware_version(0x0200, None, {}) == "2.0"


@pytest.mark.parametrize(
    ("texts", "words", "expected"),
    [
        pytest.param(PCU_WARNING_TEXTS, [0, 0, 0, 0], "None", id="no-bits"),
        pytest.param(PCU_WARNING_TEXTS, [0x4000, 0x0001, 0, 0], "Discharge prohibited, Bus overvoltage", id="multiple-words"),
        pytest.param(PCU_FAULT_TEXTS, [0, 0, 0, 0x0001], "Unknown bit 4.0", id="undocumented-bit"),
        pytest.param({}, 0x0009, "Unknown bit 0, Unknown bit 3", id="undocumented-single-register"),
        pytest.param(BDU_FAULT_TEXTS, 0x0002, "Fuse fault", id="single-register"),
    ],
)
def test_bit_texts(texts: dict[tuple[int, int], str], words: Any, expected: str) -> None:
    assert value_function_bit_texts(texts)(words, None, {}) == expected


async def test_no_levels_without_bts() -> None:
    hub = FakeHub({})
    hub._invertertype = HYBRID | X3

    assert await battery_config().async_get_extended_levels(hub) == []
    assert hub.writes == []


async def test_single_tower_scopes_pcus_to_the_tower() -> None:
    config, instances = await _discover(FakeHub({0x6084: 1, 0x6004: 4}))

    assert [(i.key, i.name, i.key_prefix, i.number, i.parent_key) for i in instances["BDU"]] == [
        ("battery_1", "Battery 1", "battery_1_bdu_", 1, None)
    ]
    assert [(i.key, i.name, i.key_prefix, i.number, i.parent_key) for i in instances["PCU"]] == [
        (f"battery_1_pcu_{n}", f"Battery 1 PCU {n}", f"battery_1_pcu_{n}_", n, "battery_1") for n in range(1, 5)
    ]
    assert [config.get_pack_parent_key(batt_nr) for batt_nr in (0, 1)] == ["battery_1", None]


@pytest.mark.parametrize(
    ("devices", "expected_towers"),
    [
        pytest.param({1: BDU_SERIAL}, ["battery_1"], id="bdu-answers"),
        pytest.param({}, [], id="no-bdu"),
    ],
)
async def test_bdu_probed_when_count_reads_zero(devices: dict[int, list[int]], expected_towers: list[str]) -> None:
    # After an inverter restart BDU_Number (0x6084) reads 0 while the BDU answers.
    _, instances = await _discover(FakeHub({0x6084: 0, 0x6090: 2, 0x6004: 4}, devices))

    assert [i.key for i in instances["BDU"]] == expected_towers


async def test_multiple_towers_skip_pcus(caplog: pytest.LogCaptureFixture) -> None:
    config, instances = await _discover(FakeHub({0x6084: 2, 0x6004: 8}))

    assert [i.key for i in instances["BDU"]] == ["battery_1", "battery_2"]
    assert instances["PCU"] == []
    assert config.get_pack_parent_key(0) is None
    assert "2 battery towers found" in caplog.text


async def test_select_skips_write_when_already_selected() -> None:
    hub = FakeHub({0x6090: 1})

    assert await _bdu_level().select(hub, _instance(1)) is True
    assert hub.writes == []


@pytest.mark.parametrize(("follow_selection", "expected"), [(True, True), (False, False)], ids=["selected", "not-selected"])
async def test_select_writes_query_register_and_checks_readback(follow_selection: bool, expected: bool) -> None:
    hub = FakeHub({0x6090: 1}, follow_selection=follow_selection)

    assert await _bdu_level().select(hub, _instance(2)) is expected
    assert hub.writes == [(0x60C4, 2)]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param({"battery_1_bdu_id": 1, "battery_1_bdu_serial_number": "SH2005050KE228164269"}, True, id="valid"),
        pytest.param({"battery_1_bdu_id": 2, "battery_1_bdu_serial_number": "SH2005050KE228164269"}, False, id="wrong-id"),
        pytest.param({"battery_1_bdu_id": 1, "battery_1_bdu_serial_number": ""}, False, id="missing-serial"),
    ],
)
async def test_verify_requires_matching_id_and_serial(data: dict[str, Any], expected: bool) -> None:
    assert await _bdu_level().verify(FakeHub({}), _instance(1), data) is expected


def test_device_details_skip_missing_values() -> None:
    data = {"battery_1_pcu_4_serial_number": "SQ200402K5C233061682", "battery_1_pcu_4_software_version": "V01.00.33"}

    assert _bdu_level().get_device_details(data, "battery_1_pcu_4_") == {
        "serial_number": "SQ200402K5C233061682",
        "sw_version": "V01.00.33",
    }


def test_balancing_cells_counts_active_bits() -> None:
    assert value_function_bit_count(0b1000_0000_0000_0101, None, {}) == 3


@pytest.mark.parametrize(
    ("invertertype", "expected"),
    [
        pytest.param(HYBRID | X3 | BAT_BTS, BATTERY_EXTENDED_SENSOR_TYPES, id="bts"),
        pytest.param(HYBRID | X3, [], id="without-bts"),
    ],
)
def test_extended_pack_sensors_only_for_bts(invertertype: int, expected: list[Any]) -> None:
    hub = SimpleNamespace(_invertertype=invertertype)

    assert battery_config().get_extended_pack_sensor_types(hub) == expected
