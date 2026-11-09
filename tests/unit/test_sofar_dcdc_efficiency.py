"""DC/DC loss and efficiency of a SOFAR BTS tower from a sliding window."""

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.solax_modbus import plugin_sofar
from custom_components.solax_modbus.const import battery_device_instance, is_inputless_computed_sensor
from custom_components.solax_modbus.plugin_sofar import (
    BAT_BTS,
    DCDC_MIN_SAMPLES,
    DCDC_SENSOR_TYPES,
    DCDC_WINDOW_SECONDS,
    HYBRID,
    X3,
    battery_config,
    chain_efficiency,
    dcdc_efficiency_window,
    energy_ratio_window,
    sofar_pcu_level,
    value_function_battery_lifetime_efficiency,
    value_function_conversion_loss_today,
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
    window.add(15 * DCDC_MIN_SAMPLES, -10000, -100)

    assert window.efficiency == {"discharge": 76.7}


def test_efficiency_clears_once_the_direction_left_the_window() -> None:
    """While charging, the discharge efficiency is unknown instead of a stale value."""
    window = _filled(-451, -588)
    for second in range(15 * DCDC_MIN_SAMPLES, DCDC_WINDOW_SECONDS + 15 * DCDC_MIN_SAMPLES, 15):
        window.add(second, 2000, 1900)

    assert window.efficiency == {"charge": 95.0}


def test_energy_ratio_efficiency_clears_without_counting_samples() -> None:
    window = energy_ratio_window()
    for second in range(DCDC_MIN_SAMPLES):
        window.add(second * 15, 513, 500)
    for second in range(DCDC_MIN_SAMPLES, DCDC_MIN_SAMPLES + DCDC_WINDOW_SECONDS // 15):
        window.add(second * 15, 5000, 4800, counts=False)

    assert window.efficiency is None


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


@pytest.mark.parametrize(
    ("samples", "efficiency", "loss"),
    [
        pytest.param([(513, 500, True)], 97.5, 13, id="battery-to-ac"),
        pytest.param([(513, 500, True), (5000, 4800, False)], 97.5, 106, id="excluded-samples-only-count-for-loss"),
        pytest.param([(40, 30, True)], None, 10, id="below-min-power"),
        pytest.param([(500, 600, True)], None, -100, id="skewed-above-100-percent"),
    ],
)
def test_energy_ratio_window(samples: list[tuple[float, float, bool]], efficiency: float | None, loss: float) -> None:
    window = energy_ratio_window()
    for second in range(DCDC_MIN_SAMPLES * len(samples)):
        power_in, power_out, counts = samples[second % len(samples)]
        window.add(second, power_in, power_out, counts)

    assert window.efficiency == efficiency
    assert window.loss == loss


class InverterHub:
    """Inverter answering the efficiency reads with the evening values measured on the real system."""

    def __init__(self, invertertype: int) -> None:
        self._modbus_addr = 1
        self._invertertype = invertertype
        self.reads: list[int] = []
        self.registers = {0x485: 50, 0x604: 3955, 0x605: 0x10000 - 130}  # 500 W AC, 395.5 V * -1.30 A

    async def async_read_holding_registers(self, unit: int, address: int, count: int) -> SimpleNamespace:
        self.reads.append(address)
        return SimpleNamespace(registers=[self.registers.get(address + i, 0) for i in range(count)], isError=lambda: False)


@pytest.mark.asyncio
async def test_inverter_efficiency_from_battery_discharge(monkeypatch: pytest.MonkeyPatch) -> None:
    config = battery_config()
    config.pcu_level = _level(1)
    config.pcu_level.window.efficiency["discharge"] = 76.7
    hub = InverterHub(HYBRID | X3 | BAT_BTS)
    clock = iter(range(0, 10_000, 15))
    monkeypatch.setattr(plugin_sofar, "wall_clock", lambda: next(clock))
    data: dict[str, Any] = {}

    for _ in range(DCDC_MIN_SAMPLES):
        await config.async_inverter_follow_up(hub, data)

    assert data["conversion_loss"] == 14
    assert data["conversion_efficiency"] == 97.2
    assert data["battery_1_bdu_battery_to_ac_efficiency"] == round(76.7 * 97.2 / 100, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invertertype", "times", "expected_samples"),
    [
        pytest.param(HYBRID | X3 | BAT_BTS, [0, 5, 10], 2, id="throttled-to-10-seconds"),
        pytest.param(HYBRID | X3, [0, 15], 0, id="without-bts"),
    ],
)
async def test_inverter_sampling(monkeypatch: pytest.MonkeyPatch, invertertype: int, times: list[int], expected_samples: int) -> None:
    """Power is sampled at most every 10 s; the depth of discharge only every 10 minutes."""
    config = battery_config()
    hub = InverterHub(invertertype)
    clock = iter(times)
    monkeypatch.setattr(plugin_sofar, "wall_clock", lambda: next(clock) + 100)

    for _ in times:
        await config.async_inverter_follow_up(hub, {})

    assert len(config.inverter_window.samples) == expected_samples
    assert hub.reads == ([0x104D] if expected_samples else []) + [0x485, 0x584, 0x604] * expected_samples


@pytest.mark.asyncio
async def test_window_drops_samples_from_before_host_suspend(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a suspend the stale low-side readings and window samples must not be reused."""
    level = _level(2)
    clock = iter([0, 1, 36_000, 36_001])
    monkeypatch.setattr(plugin_sofar, "wall_clock", lambda: next(clock))

    for number in (1, 2):
        await level.verify(None, level.instances[number - 1], _pcu_data(number, -5.0))
    assert [sample[0] for sample in level.window.samples] == [1]

    # Ten hours later PCU 1 alone must not pair with the stale PCU 2 reading.
    await level.verify(None, level.instances[0], _pcu_data(1, -5.0))
    assert [sample[0] for sample in level.window.samples] == [1]

    await level.verify(None, level.instances[1], _pcu_data(2, -5.0))
    assert [sample[0] for sample in level.window.samples] == [36_001]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registers", "dcdc", "expected"),
    [
        pytest.param(
            {0x485: 50, 0x604: 3955, 0x605: 0x10000 - 130},
            {"discharge": 76.7},
            {"battery_to_ac_efficiency": round(76.7 * 97.2 / 100, 1), "grid_to_battery_efficiency": None, "round_trip_efficiency": None},
            id="battery-discharge",
        ),
        pytest.param(
            {0x485: 0x10000 - 200, 0x604: 4000, 0x605: 475},  # 2000 W from the grid, 400 V * 4.75 A into the battery
            {"charge": 95.0},
            {"battery_to_ac_efficiency": None, "grid_to_battery_efficiency": round(95.0 * 95.0 / 100, 1), "round_trip_efficiency": None},
            id="grid-charge",
        ),
        pytest.param(
            {0x485: 0x10000 - 200, 0x584: 3000, 0x585: 100, 0x604: 4000, 0x605: 475},  # 300 V * 1 A PV
            {"charge": 95.0},
            {"battery_to_ac_efficiency": None, "grid_to_battery_efficiency": None, "round_trip_efficiency": None},
            id="pv-excludes-grid-charge",
        ),
    ],
)
async def test_battery_chain_efficiencies(
    monkeypatch: pytest.MonkeyPatch, registers: dict[int, int], dcdc: dict[str, float], expected: dict[str, float | None]
) -> None:
    config = battery_config()
    config.pcu_level = _level(1)
    config.pcu_level.window.efficiency.update(dcdc)
    hub = InverterHub(HYBRID | X3 | BAT_BTS)
    hub.registers = registers
    clock = iter(range(0, 10_000, 15))
    monkeypatch.setattr(plugin_sofar, "wall_clock", lambda: next(clock))
    data: dict[str, Any] = {}
    for _ in range(DCDC_MIN_SAMPLES):
        await config.async_inverter_follow_up(hub, data)

    assert {key: data[f"battery_1_bdu_{key}"] for key in expected} == expected


@pytest.mark.parametrize(
    ("efficiencies", "expected"),
    [
        pytest.param((90.25, 91.2), 82.3, id="round-trip"),
        pytest.param((90.25, None), None, id="one-stage-unknown"),
    ],
)
def test_chain_efficiency(efficiencies: tuple[float | None, ...], expected: float | None) -> None:
    assert chain_efficiency(*efficiencies) == expected


DISCHARGE = {0x485: 50, 0x604: 3955, 0x605: 0x10000 - 130}  # 500 W AC from 514 W battery, 97.2 %
PV_CHARGE = {0x485: 30, 0x584: 4000, 0x585: 500, 0x604: 4000, 0x605: 400}  # 2000 W PV, 300 W AC, 1600 W battery


async def _run(config: battery_config, monkeypatch: pytest.MonkeyPatch, phases: list[dict[int, int]]) -> dict[str, Any]:
    clock = iter(range(0, 100_000, 15))
    monkeypatch.setattr(plugin_sofar, "wall_clock", lambda: next(clock))
    hub = InverterHub(HYBRID | X3 | BAT_BTS)
    data: dict[str, Any] = {}
    for registers in phases:
        hub.registers = registers
        for _ in range(DCDC_MIN_SAMPLES + DCDC_WINDOW_SECONDS // 15):
            await config.async_inverter_follow_up(hub, data)
    return data


def _config() -> battery_config:
    config = battery_config()
    config.pcu_level = _level(1)
    config.pcu_level.window.efficiency.update({"charge": 95.0, "discharge": 76.7})
    return config


@pytest.mark.asyncio
async def test_pv_to_battery_removes_the_ac_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PV used for the AC output is estimated with the DC to AC efficiency measured on discharge."""
    data = await _run(_config(), monkeypatch, [DISCHARGE, PV_CHARGE])

    inverter = round(100 * 1600 / (2000 - 300 * 100 / 97.2), 1)
    assert data["battery_1_bdu_pv_to_battery_efficiency"] == chain_efficiency(inverter, 95.0)
    assert data["battery_1_bdu_battery_to_ac_efficiency"] is None
    assert data["battery_1_bdu_round_trip_efficiency"] == chain_efficiency(chain_efficiency(inverter, 95.0), chain_efficiency(76.7, 97.2))


@pytest.mark.asyncio
async def test_pv_to_battery_needs_a_measured_dc_to_ac_efficiency(monkeypatch: pytest.MonkeyPatch) -> None:
    data = await _run(_config(), monkeypatch, [PV_CHARGE])

    assert data["battery_1_bdu_pv_to_battery_efficiency"] is None
    assert data["battery_1_bdu_round_trip_efficiency"] is None


TODAY = {
    "solar_generation_today": 16.83,
    "battery_output_energy_today": 6.08,
    "import_energy_today": 22.5,
    "load_consumption_today": 34.84,
    "export_energy_today": 0.33,
    "battery_input_energy_today": 9.51,
}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param({}, 0.73, id="counters-of-the-real-system"),
        pytest.param({"solar_generation_today": 0.0}, None, id="counter-already-reset-at-midnight"),
    ],
)
def test_conversion_loss_today(changes: dict[str, float], expected: float | None) -> None:
    assert value_function_conversion_loss_today(None, None, TODAY | changes) == expected


@pytest.mark.parametrize(
    ("charged", "expected"),
    [pytest.param(9961.0, 72.4, id="real-system"), pytest.param(0.0, None, id="never-charged")],
)
def test_battery_lifetime_efficiency(charged: float, expected: float | None) -> None:
    data = {"battery_input_energy_total": charged, "battery_output_energy_total": 7212.4}

    assert value_function_battery_lifetime_efficiency(None, None, data) == expected


@pytest.mark.asyncio
async def test_all_four_inputs_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """MPPT 3 and battery input 4 feed the balance on models that have them."""
    config = battery_config()
    hub = InverterHub(HYBRID | X3 | BAT_BTS)
    # 1000 W AC from 600 W on MPPT 3 and 430 W from battery input 4
    hub.registers = {0x485: 100, 0x58A: 3000, 0x58B: 200, 0x619: 4300, 0x61A: 0x10000 - 100}
    clock = iter(range(0, 10_000, 15))
    monkeypatch.setattr(plugin_sofar, "wall_clock", lambda: next(clock))
    data: dict[str, Any] = {}
    for _ in range(DCDC_MIN_SAMPLES):
        await config.async_inverter_follow_up(hub, data)

    assert data["conversion_loss"] == 30
