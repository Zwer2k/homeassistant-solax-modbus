"""Tests for SOFAR BTS pack registers, using values read from a HYD10KTL-3P 2nd."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.solax_modbus.const import REGISTER_STR, REGISTER_U32, value_function_2byte_timestamp
from custom_components.solax_modbus.plugin_sofar import (
    BAT_BTS,
    BATTERY_SENSOR_TYPES,
    GEN,
    HYBRID,
    HYD_EP,
    X1,
    X3,
    battery_config,
    plugin_instance,
    soc_capacity_fit,
)

# Pack_RT_SN0..SN9 (0x9047-0x9050) of a BTS 5K module
PACK_SERIAL_REGISTERS = [0x5348, 0x3230, 0x3034, 0x3430, 0x3056, 0x4532, 0x3333, 0x3231, 0x3333, 0x3239]

DESCRIPTIONS = {description.key: description for description in BATTERY_SENSOR_TYPES}


def test_pack_serial_number_includes_sn0() -> None:
    """The serial starts at Pack_RT_SN0 (0x9047), not at SN1."""
    description = DESCRIPTIONS["pack_serial_number"]

    assert description.register == 0x9047
    assert description.register_data_type == REGISTER_STR
    assert description.wordcount == 10
    assert battery_config.batt_pack_serial_address == 0x9047
    assert battery_config.batt_pack_serial_len == 10


@pytest.mark.asyncio
async def test_determinate_batt_pack_serial_reads_full_serial() -> None:
    """Serial detection during setup returns the full 20 character serial."""
    hub = SimpleNamespace(
        _modbus_addr=1,
        async_read_holding_registers=AsyncMock(return_value=SimpleNamespace(registers=PACK_SERIAL_REGISTERS, isError=lambda: False)),
    )

    assert await battery_config()._determinate_batt_pack_serial(hub) == "SH2004400VE233213329"
    hub.async_read_holding_registers.assert_awaited_once_with(unit=1, address=0x9047, count=10)


def test_pack_time_is_unsigned_32_bit_timestamp() -> None:
    """Pack_RT_Timestamp is a U32 bit field over 0x9045-0x9046."""
    description = DESCRIPTIONS["pack_time"]

    assert description.register == 0x9045
    assert description.register_data_type == REGISTER_U32
    assert value_function_2byte_timestamp(0x6AC80ED8, description, {}) == datetime(2026, 11, 4, 0, 59, 24)


def test_bms_version_has_no_state_class() -> None:
    """A version number must not produce long-term statistics."""
    assert DESCRIPTIONS["bms_version"].state_class is None


@pytest.mark.parametrize(
    ("invertertype", "serial", "expected_names"),
    [
        pytest.param(HYBRID | X3 | GEN | BAT_BTS, "SP2ES110N6T029", ["Module Voltage", "Module Current"], id="bts-per-module"),
        pytest.param(HYBRID | X1 | GEN | HYD_EP, "SM2ES4", ["Total Voltage", "Total Current"], id="hyd-ep-unchanged"),
    ],
)
def test_battery_voltage_and_current_names_per_model(invertertype: int, serial: str, expected_names: list[str]) -> None:
    """Each model gets exactly one variant of the shared keys."""
    names = [
        descr.name
        for descr in BATTERY_SENSOR_TYPES
        if descr.key in ("total_voltage", "total_current")
        and plugin_instance.matchInverterWithMask(invertertype, descr.allowedtypes, serial, descr.blacklist)
    ]

    assert names == expected_names


@pytest.mark.parametrize(
    ("soc_values", "expected"),
    [
        pytest.param(range(10, 91, 5), 0.904, id="learned"),
        pytest.param(range(40, 59), None, id="soc-span-too-small"),
    ],
)
def test_soc_capacity_fit_learns_ah_per_percent(soc_values: range, expected: float | None) -> None:
    fit = soc_capacity_fit()
    for soc in soc_values:
        fit.add(soc, 17 + 0.904 * soc)  # module 1/1: 17 Ah below SOC 0 % (BMS reserve)

    assert fit.ah_per_percent == (pytest.approx(expected) if expected else None)


PACK_DATA = {
    "battery_1_1_soc": 33,
    "battery_1_1_pack_remaining_capacity": 49.2,
    "battery_1_1_pack_full_charge_capacity": 104.8,
    "battery_1_1_total_voltage": 52.5,
}


@pytest.mark.parametrize(
    ("depth_of_discharge", "remaining", "usable"),
    [
        # 0.86 * 104.8 Ah / 100 = 0.901 Ah per % until enough SOC span is learned
        pytest.param(90, 1.09, 4.26, id="above-discharge-limit"),
        pytest.param(60, 0.0, 2.84, id="below-discharge-limit"),
        pytest.param(None, None, None, id="depth-of-discharge-unknown"),
    ],
)
def test_pack_energy_excludes_reserves(depth_of_discharge: int | None, remaining: float | None, usable: float | None) -> None:
    config = battery_config(depth_of_discharge=depth_of_discharge)
    data = dict(PACK_DATA)

    config._update_pack_energy(data, "battery_1_1_")

    assert data["battery_1_1_remaining_energy"] == remaining
    assert data["battery_1_1_usable_energy"] == usable
