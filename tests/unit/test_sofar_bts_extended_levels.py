"""Tests for the SOFAR BTS BDU and PCU device levels."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

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
    """Inverter whose query register selects the instance reported in the ID register."""

    def __init__(self, registers: dict[int, int], instances: dict[int, list[int]], follow_selection: bool = True) -> None:
        self._modbus_addr = 1
        self._invertertype = HYBRID | X3 | BAT_BTS
        self.registers = registers
        self.instances = instances
        self.follow_selection = follow_selection
        self.writes: list[tuple[int, int]] = []

    async def async_read_holding_registers(self, unit: int, address: int, count: int) -> SimpleNamespace:
        return SimpleNamespace(registers=[self.registers.get(address + i, 0) for i in range(count)], isError=lambda: False)

    async def async_write_registers_single(self, unit: int, address: int, payload: int) -> None:
        self.writes.append((address, payload))
        self.registers[address] = payload
        if self.follow_selection:
            self.registers[0x6090] = payload
            for i, value in enumerate(self.instances.get(payload, [0] * 10)):
                self.registers[0x6091 + i] = value


async def _levels(invertertype: int = HYBRID | X3 | BAT_BTS) -> dict[str, sofar_selectable_level]:
    levels = await battery_config().async_get_extended_levels(SimpleNamespace(_invertertype=invertertype))
    return {level.key: level for level in levels if isinstance(level, sofar_selectable_level)}


async def _bdu_level() -> sofar_selectable_level:
    return (await _levels())["battery_bdu_{nr}"]


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


@pytest.mark.parametrize(
    ("invertertype", "expected_keys"),
    [
        pytest.param(HYBRID | X3 | BAT_BTS, ["battery_bdu_{nr}", "battery_pcu_{nr}"], id="bts"),
        pytest.param(HYBRID | X3, [], id="without-bts"),
    ],
)
async def test_levels_only_for_bts(invertertype: int, expected_keys: list[str]) -> None:
    assert list(await _levels(invertertype)) == expected_keys


@pytest.mark.asyncio
async def test_select_skips_write_when_already_selected() -> None:
    hub = FakeHub({0x6090: 1}, {})

    assert await (await _bdu_level()).select(hub, 0) is True
    assert hub.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("follow_selection", "expected"), [(True, True), (False, False)], ids=["selected", "not-selected"])
async def test_select_writes_query_register_and_checks_readback(follow_selection: bool, expected: bool) -> None:
    hub = FakeHub({0x6090: 1}, {}, follow_selection=follow_selection)

    assert await (await _bdu_level()).select(hub, 1) is expected
    assert hub.writes == [(0x60C4, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("instances", "expected"),
    [
        pytest.param({1: BDU_SERIAL}, 1, id="bdu-answers"),
        pytest.param({}, 0, id="no-bdu"),
    ],
)
async def test_bdu_quantity_probes_first_instance_when_count_is_zero(instances: dict[int, list[int]], expected: int) -> None:
    # After an inverter restart BDU_Number (0x6084) reads 0 while the BDU answers.
    hub = FakeHub({0x6084: 0, 0x6090: 2}, instances)

    assert await (await _bdu_level()).get_quantity(hub) == expected


@pytest.mark.asyncio
async def test_pcu_quantity_from_count_register() -> None:
    pcu_level = (await _levels())["battery_pcu_{nr}"]

    assert await pcu_level.get_quantity(FakeHub({0x6004: 4}, {})) == 4


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param({"battery_bdu_1_id": 1, "battery_bdu_1_serial_number": "SH2005050KE228164269"}, True, id="valid"),
        pytest.param({"battery_bdu_1_id": 2, "battery_bdu_1_serial_number": "SH2005050KE228164269"}, False, id="wrong-id"),
        pytest.param({"battery_bdu_1_id": 1, "battery_bdu_1_serial_number": ""}, False, id="missing-serial"),
    ],
)
async def test_verify_requires_matching_id_and_serial(data: dict[str, Any], expected: bool) -> None:
    assert await (await _bdu_level()).verify(None, 0, data, "battery_bdu_1_") is expected


async def test_device_details_skip_missing_values() -> None:
    data = {"battery_pcu_4_serial_number": "SQ200402K5C233061682", "battery_pcu_4_software_version": "V01.00.33"}

    assert (await _bdu_level()).get_device_details(data, "battery_pcu_4_") == {
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
