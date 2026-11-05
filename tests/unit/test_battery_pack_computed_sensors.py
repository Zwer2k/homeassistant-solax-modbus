"""Computed sensors on prefixed battery devices use the inputs of their own device."""

from dataclasses import replace
from typing import Any, cast

import pytest

from custom_components.solax_modbus import SolaXModbusHub
from custom_components.solax_modbus.const import BaseModbusSensorEntityDescription
from custom_components.solax_modbus.plugin_sofar import BATTERY_EXTENDED_SENSOR_TYPES, BATTERY_SENSOR_TYPES
from custom_components.solax_modbus.sensor import _prefix_local_dependencies

LOCAL_KEYS = {"cell_max_voltage", "cell_min_voltage"}


@pytest.mark.parametrize(
    ("field", "dependencies", "expected"),
    [
        pytest.param("depends_on", ["cell_max_voltage", "inverter_key"], ["battery_1_2_cell_max_voltage", "inverter_key"], id="required"),
        pytest.param("optional_depends_on", ["cell_min_voltage"], ["battery_1_2_cell_min_voltage"], id="optional"),
        pytest.param(
            "depends_on_any",
            [("cell_max_voltage", "inverter_key")],
            [("battery_1_2_cell_max_voltage", "inverter_key")],
            id="alternatives",
        ),
    ],
)
def test_only_dependencies_on_the_same_device_are_prefixed(field: str, dependencies: Any, expected: Any) -> None:
    descr = BaseModbusSensorEntityDescription(key="battery_1_2_derived", **{field: dependencies})

    assert getattr(_prefix_local_dependencies(descr, "battery_1_2_", LOCAL_KEYS), field) == expected


def test_description_without_dependencies_is_unchanged() -> None:
    descr = BaseModbusSensorEntityDescription(key="battery_1_2_soc")

    assert _prefix_local_dependencies(descr, "battery_1_2_", LOCAL_KEYS) is descr


def test_derived_pack_sensors_depend_on_pack_sensors() -> None:
    pack_keys = {str(descr.key) for descr in BATTERY_SENSOR_TYPES + BATTERY_EXTENDED_SENSOR_TYPES}

    for descr in BATTERY_EXTENDED_SENSOR_TYPES:
        assert set(descr.depends_on or []) <= pack_keys


def test_pack_sensors_are_computed_from_their_own_pack() -> None:
    """Two packs with identical derived keys must not mix their inputs."""
    pack_keys = {str(descr.key) for descr in BATTERY_SENSOR_TYPES + BATTERY_EXTENDED_SENSOR_TYPES}
    hub = cast(Any, object.__new__(SolaXModbusHub))
    hub._name = "Sofar"
    hub.sensorDescriptions = {}
    hub.computedSensors = {}
    for prefix in ("battery_1_1_", "battery_1_2_"):
        for descr in BATTERY_SENSOR_TYPES + BATTERY_EXTENDED_SENSOR_TYPES:
            prefixed = _prefix_local_dependencies(replace(descr, key=prefix + str(descr.key)), prefix, pack_keys)
            hub.sensorDescriptions[prefixed.key] = prefixed
            if prefixed.value_function:
                hub.computedSensors[prefixed.key] = prefixed
    data: dict[str, Any] = {
        "battery_1_1_cell_max_voltage": 3.282,
        "battery_1_1_cell_min_voltage": 3.279,
        "battery_1_1_pack_remaining_capacity": 49.2,
        "battery_1_1_pack_full_charge_capacity": 104.8,
        "battery_1_1_total_voltage": 52.5,
        "battery_1_2_cell_max_voltage": 3.4,
        "battery_1_2_cell_min_voltage": 3.3,
    }
    fresh = set(data)

    assert hub._compute_poll_sensors(data, fresh) == {
        "battery_1_1_cell_voltage_difference",
        "battery_1_1_remaining_energy",
        "battery_1_1_full_charge_energy",
        "battery_1_2_cell_voltage_difference",
    }
    assert data["battery_1_1_cell_voltage_difference"] == 0.003
    assert data["battery_1_2_cell_voltage_difference"] == 0.1
    assert data["battery_1_1_remaining_energy"] == 2.58
    assert data["battery_1_1_full_charge_energy"] == 5.5
