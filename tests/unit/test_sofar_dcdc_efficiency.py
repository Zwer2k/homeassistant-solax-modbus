"""DC/DC loss and efficiency of a SOFAR BTS tower from a sliding window."""

from typing import Any

import pytest

from custom_components.solax_modbus.const import battery_device_instance, is_inputless_computed_sensor
from custom_components.solax_modbus.plugin_sofar import (
    DCDC_MIN_SAMPLES,
    DCDC_SENSOR_TYPES,
    DCDC_WINDOW_SECONDS,
    dcdc_efficiency_window,
    sofar_pcu_level,
)
from custom_components.solax_modbus.sensor import _prefix_local_dependencies


def _filled(high: float, low: float, count: int = DCDC_MIN_SAMPLES) -> dcdc_efficiency_window:
    window = dcdc_efficiency_window()
    for second in range(count):
        window.add(second * 15, high, low)
    return window


@pytest.mark.parametrize(
    ("high", "low", "direction", "efficiency", "loss"),
    [
        pytest.param(-451, -588, "discharge", 76.7, 137, id="discharge"),
        pytest.param(2000, 1900, "charge", 95.0, 100, id="charge"),
    ],
)
def test_efficiency_and_loss(high: float, low: float, direction: str, efficiency: float, loss: float) -> None:
    window = _filled(high, low)

    assert window.efficiency == {direction: efficiency}
    assert window.loss == loss


def test_efficiency_is_an_energy_ratio() -> None:
    """A strong sample weighs more than a weak one, unlike a mean of ratios."""
    window = dcdc_efficiency_window()
    for second in range(DCDC_MIN_SAMPLES):
        window.add(second, -900, -1000) if second % 2 else window.add(second, -50, -100)

    assert window.efficiency["discharge"] == round(100 * (950 * 5) / (1100 * 5), 1)


@pytest.mark.parametrize(
    ("high", "low", "count"),
    [
        pytest.param(-20, -40, DCDC_MIN_SAMPLES, id="below-min-power"),
        pytest.param(-451, -588, DCDC_MIN_SAMPLES - 1, id="too-few-samples"),
    ],
)
def test_no_efficiency_without_reliable_window(high: float, low: float, count: int) -> None:
    window = _filled(high, low, count)

    assert window.efficiency == {}


def test_last_valid_efficiency_survives_skewed_window() -> None:
    window = _filled(-451, -588)
    for second in range(2 * DCDC_WINDOW_SECONDS, 2 * DCDC_WINDOW_SECONDS + 15 * DCDC_MIN_SAMPLES, 15):
        window.add(second, -600, -500)

    assert window.efficiency == {"discharge": 76.7}


def test_old_samples_leave_the_window() -> None:
    window = _filled(-451, -588)
    window.add(DCDC_WINDOW_SECONDS + 1000, 500, 450)

    assert len(window.samples) == 1
    assert window.loss is None


def test_tower_sensors_are_inputless_after_prefixing() -> None:
    for descr in DCDC_SENSOR_TYPES:
        assert is_inputless_computed_sensor(_prefix_local_dependencies(descr, "battery_1_bdu_", {"dcdc_loss"}))


def _level(pcus: int) -> sofar_pcu_level:
    level = sofar_pcu_level(name="PCU", sensor_types=[], inquire_address=0x6044, id_address=0x6010, tower_key_prefix="battery_1_bdu_")
    level.instances = [
        battery_device_instance(key=f"battery_1_pcu_{n}", name=f"Battery 1 PCU {n}", key_prefix=f"battery_1_pcu_{n}_", number=n)
        for n in range(1, pcus + 1)
    ]
    return level


def _pcu_data(number: int, low_current: float) -> dict[str, Any]:
    prefix = f"battery_1_pcu_{number}_"
    return {
        f"{prefix}id": number,
        f"{prefix}serial_number": f"SN{number}",
        f"{prefix}low_side_voltage": 50.0,
        f"{prefix}low_side_current": low_current,
        f"{prefix}battery_voltage_1": 400.0,
        f"{prefix}battery_current_1": -1.0,
        f"{prefix}battery_voltage_2": 0.0,
        f"{prefix}battery_current_2": 0.0,
    }


@pytest.mark.asyncio
async def test_pcu_reads_feed_the_tower_window() -> None:
    """Samples start once every PCU was read; each pairs fresh high-side power with all PCUs."""
    level = _level(2)
    first = _pcu_data(1, -5.0)

    assert await level.verify(None, level.instances[0], first) is True
    assert list(level.window.samples) == []
    assert "battery_1_bdu_dcdc_loss" not in first

    second = _pcu_data(2, -5.0)
    assert await level.verify(None, level.instances[1], second) is True

    assert [(high, low) for _t, high, low in level.window.samples] == [(-400.0, -500.0)]
    assert second["battery_1_bdu_dcdc_loss"] is None
    assert second["battery_1_bdu_dcdc_efficiency_discharge"] is None


@pytest.mark.asyncio
async def test_rejected_pcu_read_does_not_feed_the_window() -> None:
    level = _level(1)
    data = _pcu_data(1, -5.0)
    data["battery_1_pcu_1_id"] = 2

    assert await level.verify(None, level.instances[0], data) is False
    assert list(level.window.samples) == []
