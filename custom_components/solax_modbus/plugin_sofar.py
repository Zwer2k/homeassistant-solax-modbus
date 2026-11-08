import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from time import time as wall_clock  # keeps running while the host is suspended, unlike monotonic
from typing import Any

from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.helpers.entity import EntityCategory  # type: ignore[attr-defined]  # HA stubs incomplete

from custom_components.solax_modbus.const import (
    CONF_READ_DCB,
    CONF_READ_EPS,
    CONF_READ_PM,
    DEFAULT_READ_DCB,
    DEFAULT_READ_EPS,
    DEFAULT_READ_PM,
    REG_HOLDING,
    REGISTER_S16,
    REGISTER_S32,
    REGISTER_STR,
    REGISTER_U16,
    REGISTER_U32,
    REGISTER_WORDS,
    SCAN_GROUP_DEFAULT,
    SCAN_GROUP_FAST,
    SCAN_GROUP_MEDIUM,
    WRITE_DATA_LOCAL,
    WRITE_MULTI_MODBUS,
    WRITE_MULTISINGLE_MODBUS,
    BaseModbusButtonEntityDescription,
    BaseModbusNumberEntityDescription,
    BaseModbusSelectEntityDescription,
    BaseModbusSensorEntityDescription,
    UnitOfReactivePower,
    base_battery_config,
    battery_device_instance,
    battery_device_level,
    plugin_base,
    value_function_2byte_timestamp,
    value_function_disabled_enabled,
    value_function_rtc_ymd,
)

from .pymodbus_compat import DataType, convert_from_registers

_LOGGER = logging.getLogger(__name__)

_UNINITIALIZED_SELECT_DEFAULTS: dict[str, int] = {
    "feedin_limitation_mode": 0,
    "passive_mode_timeout": 0,
    "passive_mode_timeout_action": 0,
}

""" ============================================================================================
bitmasks  definitions to characterize inverters, organized by group
these bitmasks are used in entity declarations to determine to which inverters the entity applies
within a group, the bits in an entity declaration will be interpreted as OR
between groups, an AND condition is applied, so all gruoups must match.
An empty group (group without active flags) evaluates to True.
example: GEN3 | GEN4 | X1 | X3 | EPS
means:  any inverter of type (GEN3 or GEN4) and (X1 or X3) and (EPS)
An entity can be declared multiple times (with different bitmasks) if the parameters are different for each inverter type
"""

GEN = 0x0001  # base generation for MIC, PV, AC
GEN2 = 0x0002
GEN3 = 0x0004
GEN4 = 0x0008
ALL_GEN_GROUP = GEN2 | GEN3 | GEN4 | GEN

X1 = 0x0100
X3 = 0x0200
ALL_X_GROUP = X1 | X3

PV = 0x0400  # Needs further work on PV Only Inverters
AC = 0x0800
HYBRID = 0x1000
MIC = 0x2000
ALL_TYPE_GROUP = PV | AC | HYBRID | MIC

EPS = 0x8000
ALL_EPS_GROUP = EPS

DCB = 0x10000  # dry contact box - gen4
ALL_DCB_GROUP = DCB

PM = 0x20000
ALL_PM_GROUP = PM

MPPT3 = 0x40000
MPPT4 = 0x80000
MPPT6 = 0x100000
MPPT8 = 0x200000
MPPT10 = 0x400000
ALL_MPPT_GROUP = MPPT3 | MPPT4 | MPPT6 | MPPT8 | MPPT10

BAT_BTS = 0x1000000

HYD_EP = 0x2000000
ALL_MODEL_GROUP = HYD_EP

HYD_EP_CURRENT_SCALE_EXCEPTIONS = [("SM2ES4", 0.1)]

ALLDEFAULT = 0  # should be equivalent to HYBRID | AC | GEN2 | GEN3 | GEN4 | X1 | X3

# ======================= end of bitmask handling code =============================================

# ====================== find inverter type and details ===========================================


async def async_read_serialnr(hub: Any, address: int, swapbytes: bool) -> str | None:
    res = None
    try:
        inverter_data = await hub.async_read_holding_registers(unit=hub._modbus_addr, address=address, count=7)
        if inverter_data is not None and not inverter_data.isError():
            raw = convert_from_registers(inverter_data.registers[0:7], DataType.STRING, "big")  # type: ignore[attr-defined]  # DataType enum dynamic
            res = raw.decode("ascii", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
            if swapbytes:
                ba = bytearray(res, "ascii")  # convert to bytearray for swapping
                ba[0::2], ba[1::2] = ba[1::2], ba[0::2]  # swap bytes ourselves - due to bug in Endian.LITTLE ?
                res = str(ba, "ascii")  # convert back to string
            hub.seriesnumber = res
    except Exception:
        _LOGGER.warning("%s: attempt to read serialnumber failed at 0x%x", hub.name, address, exc_info=True)
    if not res:
        _LOGGER.warning("%s: reading serial number from address 0x%x failed; other address may succeed", hub.name, address)
    _LOGGER.info("Read %s 0x%x serial number: %s, swapped: %s", hub.name, address, res, swapbytes)
    # return 'SP1ES2'
    return res


# =================================================================================================


@dataclass(kw_only=True, frozen=True)
class SofarModbusButtonEntityDescription(BaseModbusButtonEntityDescription):
    allowedtypes: int = ALLDEFAULT  # maybe 0x0000 (nothing) is a better default choice
    # write_method = WRITE_MULTISINGLE_MODBUS


@dataclass(kw_only=True, frozen=True)
class SofarModbusNumberEntityDescription(BaseModbusNumberEntityDescription):
    allowedtypes: int = ALLDEFAULT  # maybe 0x0000 (nothing) is a better default choice
    # write_method = WRITE_MULTISINGLE_MODBUS


@dataclass(kw_only=True, frozen=True)
class SofarModbusSelectEntityDescription(BaseModbusSelectEntityDescription):
    allowedtypes: int = ALLDEFAULT  # maybe 0x0000 (nothing) is a better default choice
    # write_method = WRITE_MULTISINGLE_MODBUS


@dataclass(kw_only=True, frozen=True)
class SofarModbusSensorEntityDescription(BaseModbusSensorEntityDescription):
    """A class that describes Sofar Modbus sensor entities."""

    allowedtypes: int = ALLDEFAULT  # maybe 0x0000 (nothing) is a better default choice
    order16: str = "big"
    order32: str = "big"
    register_data_type: str = REGISTER_U16
    register_type: int = REG_HOLDING


def validate_register_data(descr: Any, value: Any, datadict: dict[str, Any]) -> Any:
    """Normalize known Sofar sentinel values before entities consume them."""
    if value == 0xFFFF and descr.key in _UNINITIALIZED_SELECT_DEFAULTS:
        normalized = _UNINITIALIZED_SELECT_DEFAULTS[descr.key]
        _LOGGER.debug("Sofar: normalizing uninitialized register value for %s from 65535 to %s", descr.key, normalized)
        return normalized
    return value


# ====================================== Computed value functions  =================================================


def value_function_passivemode(initval: Any, descr: Any, datadict: dict[str, Any]) -> Any:
    return [
        (REGISTER_S32, datadict.get("passive_mode_grid_power", 0)),
        (REGISTER_S32, datadict.get("passive_mode_battery_power_min", 0)),
        (REGISTER_S32, datadict.get("passive_mode_battery_power_max", 0)),
    ]


def value_function_passive_timeout(initval: Any, descr: Any, datadict: dict[str, Any]) -> Any:
    return [
        (
            "passive_mode_timeout",
            datadict.get("passive_mode_timeout", datadict.get("passive_mode_timeout")),
        ),
        (
            "passive_mode_timeout_action",
            datadict.get("passive_mode_timeout_action", datadict.get("passive_mode_timeout_action")),
        ),
    ]


def value_function_refluxcontrol(initval: Any, descr: Any, datadict: dict[str, Any]) -> Any:
    return [
        (
            "feedin_limitation_mode",
            datadict.get("feedin_limitation_mode", datadict.get("feedin_limitation_mode")),
        ),
        (
            "feedin_max_power",
            int(datadict.get("feedin_max_power", 0)) / 100,
        ),
    ]


def value_function_epscontrol(initval: Any, descr: Any, datadict: dict[str, Any]) -> Any:
    return [
        (
            "eps_control",
            datadict.get("eps_control", datadict.get("eps_control")),
        ),
        (
            "eps_wait_time",
            0,  # Always 0 as this is a reserved function that should not be used.
        ),
    ]


# TIMING AND TOU DISABLED AS THESE ARE NOT WORKING
# def value_function_timingmode(initval: Any, descr: Any, datadict: dict[str, Any]) -> Any:
#     return  [ ('timing_id', datadict.get('timing_id', 0), ),
#               ('timing_charge', datadict.get('timing_charge', datadict.get('ro_timing_charge')), ),
#               ('timing_charge_start_time', datadict.get('timing_charge_start_time', datadict.get('ro_timing_charge_start_time')), ),
#               ('timing_charge_end_time', datadict.get('timing_charge_end_time', datadict.get('ro_timing_charge_end_time')), ),
#               ('timing_discharge_start_time', datadict.get('timing_discharge_start_time', datadict.get('ro_timing_discharge_start_time')), ),
#               ('timing_discharge_end_time', datadict.get('timing_discharge_end_time', datadict.get('ro_timing_discharge_end_time')), ),
#               ('timing_charge_power', datadict.get('timing_charge_power', 0), ),
#               ('timing_discharge_power', datadict.get('timing_discharge_power', 0), ),
#             ]

# def value_function_toumode(initval: Any, descr: Any, datadict: dict[str, Any]) -> Any:
#     return  [ ('tou_id', datadict.get('tou_id', 0), ),
#               ('tou_control', datadict.get('tou_control', datadict.get('ro_tou_control')), ),
#               ('tou_charge_start_time', datadict.get('tou_charge_start_time', datadict.get('ro_tou_charge_start_time')), ),
#               ('tou_charge_end_time', datadict.get('tou_charge_end_time', datadict.get('ro_tou_charge_end_time')), ),
#               ('tou_target_soc', datadict.get('tou_target_soc', datadict.get('tou_target_soc')), ),
#               ('tou_charge_power', datadict.get('tou_charge_power', 0), ),
#             ]


def value_function_sync_rtc_ymd_sofar(initval: Any, descr: Any, datadict: dict[str, Any]) -> Any:
    now = datetime.now()
    return [
        (
            REGISTER_U16,
            now.year % 100,
        ),
        (
            REGISTER_U16,
            now.month,
        ),
        (
            REGISTER_U16,
            now.day,
        ),
        (
            REGISTER_U16,
            now.hour,
        ),
        (
            REGISTER_U16,
            now.minute,
        ),
        (
            REGISTER_U16,
            now.second,
        ),
        (
            REGISTER_U16,
            1,
        ),
    ]


# ================================= Button Declarations ============================================================

BUTTON_TYPES = [
    SofarModbusButtonEntityDescription(
        name="Passive: Update Battery Charge/Discharge",
        key="passive_mode_battery_charge_discharge",
        register=0x1187,
        allowedtypes=HYBRID,
        write_method=WRITE_MULTI_MODBUS,
        icon="mdi:battery-check",
        value_function=value_function_passivemode,
        depends_on=[
            "passive_mode_grid_power",
            "passive_mode_battery_power_min",
            "passive_mode_battery_power_max",
        ],
    ),
    SofarModbusButtonEntityDescription(
        name="Passive: Update Timeout",
        key="passive_mode_update_timeout",
        register=0x1184,
        allowedtypes=HYBRID,
        write_method=WRITE_MULTI_MODBUS,
        icon="mdi:timer",
        value_function=value_function_passive_timeout,
        depends_on=[
            "passive_mode_timeout",
            "passive_mode_timeout_action",
        ],
    ),
    # Unlikely to work as Sofar requires writing 7 registers, where the last needs to have the constant value of '1' during a write operation.
    SofarModbusButtonEntityDescription(
        name="Update System Time",
        key="sync_rtc",
        register=0x1004,
        allowedtypes=HYBRID | PV,
        write_method=WRITE_MULTI_MODBUS,
        icon="mdi:home-clock",
        value_function=value_function_sync_rtc_ymd_sofar,
    ),
    SofarModbusButtonEntityDescription(
        name="FeedIn: Update",
        key="feedin_limitation_mode",
        register=0x1023,
        allowedtypes=HYBRID | PV,
        write_method=WRITE_MULTI_MODBUS,
        icon="mdi:transmission-tower-import",
        value_function=value_function_refluxcontrol,
        depends_on=[
            "feedin_limitation_mode",
            "feedin_max_power",
        ],
    ),
    SofarModbusButtonEntityDescription(
        name="EPS: Update",
        key="eps_control_update",
        register=0x1029,
        allowedtypes=HYBRID,
        write_method=WRITE_MULTI_MODBUS,
        icon="mdi:power-plug-off",
        value_function=value_function_epscontrol,
        depends_on=[
            "eps_control",
            "eps_wait_time",
        ],
    ),
    SofarModbusButtonEntityDescription(
        name="IV Curve Scan",
        key="IV_curve_scan",
        register=0x1027,
        command=1,
        write_method=WRITE_MULTISINGLE_MODBUS,
        icon="mdi:chart-bell-curve",
        allowedtypes=HYBRID,
    ),
    # TIMING AND TOU DISABLED AS THESE ARE NOT WORKING
    # SofarModbusButtonEntityDescription(
    #     name = "Timing: Control",
    #     key = "timing_control",
    #     register = 0x111F,
    #     command = 1,
    #     allowedtypes = HYBRID,
    #     icon = "mdi:battery-clock",
    # ),
    # # Unlikely to work. Current value function writes just 8 registers, but according to doc 15 registers need to be written (0x1111 - 0x111F)
    # SofarModbusButtonEntityDescription(
    #     name = "TOU: Update Charge/Discharge Times",
    #     key = "update_charge_discharge_times",
    #     register = 0x1111,
    #     allowedtypes = HYBRID,
    #     write_method = WRITE_MULTI_MODBUS,
    #     icon = "mdi:battery-clock",
    #     value_function = value_function_timingmode,
    # ),
    # #Unlikely to work. According to doc starting at 0x1120 16 registers from 0x1120 to 0x112F must be written. But integration just writes 6 registers
    # SofarModbusButtonEntityDescription(
    #     name = "TOU: Update Charge Times",
    #     key = "update_tou_charge_times",
    #     register = 0x1120,
    #     allowedtypes = HYBRID,
    #     write_method = WRITE_MULTI_MODBUS,
    #     icon = "mdi:battery-clock",
    #     value_function = value_function_toumode,
    # ),
]

# ================================= Number Declarations ============================================================

NUMBER_TYPES = [
    ###
    #
    # Data only number types
    #
    ###
    SofarModbusNumberEntityDescription(
        name="Passive: Desired Grid Power",
        key="passive_mode_grid_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=NumberDeviceClass.POWER,
        register_data_type=REGISTER_S32,
        fmt="i",
        native_step=10,
        initvalue=0,
        allowedtypes=HYBRID,
        prevent_update=True,
        write_method=WRITE_DATA_LOCAL,
        icon="mdi:transmission-tower",
    ),
    SofarModbusNumberEntityDescription(
        name="Passive: Minimum Battery Power",
        key="passive_mode_battery_power_min",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=NumberDeviceClass.POWER,
        register_data_type=REGISTER_S32,
        fmt="i",
        native_max_value=20000,
        native_min_value=-20000,
        native_step=100,
        initvalue=0,
        allowedtypes=HYBRID,
        prevent_update=True,
        write_method=WRITE_DATA_LOCAL,
        icon="mdi:battery-arrow-down",
    ),
    SofarModbusNumberEntityDescription(
        name="Passive: Maximum Battery Power",
        key="passive_mode_battery_power_max",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=NumberDeviceClass.POWER,
        register_data_type=REGISTER_S32,
        fmt="i",
        native_max_value=20000,
        native_min_value=-20000,
        native_step=100,
        initvalue=0,
        allowedtypes=HYBRID,
        prevent_update=True,
        write_method=WRITE_DATA_LOCAL,
        icon="mdi:battery-arrow-up",
    ),
    SofarModbusNumberEntityDescription(
        name="FeedIn: Maximum Power",
        key="feedin_max_power",
        register_data_type=REGISTER_U16,
        fmt="i",
        native_min_value=0,
        native_max_value=20000,
        native_step=100,
        native_unit_of_measurement=UnitOfPower.WATT,
        allowedtypes=HYBRID | PV,
        prevent_update=True,
        write_method=WRITE_DATA_LOCAL,
        icon="mdi:transmission-tower-import",
    ),
    SofarModbusNumberEntityDescription(
        name="EPS Wait Time",
        key="eps_wait_time",
        register_data_type=REGISTER_U16,
        fmt="i",
        native_min_value=0,
        native_max_value=21600,
        native_step=1,
        allowedtypes=HYBRID | EPS,
        prevent_update=True,
        write_method=WRITE_DATA_LOCAL,
        entity_registry_enabled_default=False,
        icon="mdi:power-plug-off",
    ),
    # TIMING AND TOU DISABLED AS THESE ARE NOT WORKING
    # SofarModbusNumberEntityDescription(
    #     name = "Timing: Charge Power",
    #     key = "timing_charge_power",
    #     allowedtypes = HYBRID,
    #     native_min_value = 0,
    #     native_max_value = 6000,
    #     native_step = 100,
    #     native_unit_of_measurement = UnitOfPower.WATT,
    #     initvalue = 0,
    #     prevent_update = True,
    #     unit = REGISTER_U32,
    #     write_method = WRITE_DATA_LOCAL,
    # ),
    # SofarModbusNumberEntityDescription(
    #     name = "Timing: Discharge Power",
    #     key = "timing_discharge_power",
    #     allowedtypes = HYBRID,
    #     native_min_value = 0,
    #     native_max_value = 6000,
    #     native_step = 100,
    #     native_unit_of_measurement = UnitOfPower.WATT,
    #     initvalue = 0,
    #     prevent_update = True,
    #     unit = REGISTER_U32,
    #     write_method = WRITE_DATA_LOCAL,
    # ),
    # SofarModbusNumberEntityDescription(
    #     name = "Timing: ID",
    #     key = "timing_id",
    #     allowedtypes = HYBRID,
    #     native_min_value = 0,
    #     native_max_value = 4,
    #     initvalue = 0,
    #     prevent_update = True,
    #     unit = REGISTER_U16,
    #     write_method = WRITE_DATA_LOCAL,
    # ),
    # SofarModbusNumberEntityDescription(
    #     name = "TOU: ID",
    #     key = "tou_id",
    #     allowedtypes = HYBRID,
    #     native_min_value = 0,
    #     native_max_value = 4,
    #     initvalue = 0,
    #     prevent_update = True,
    #     unit = REGISTER_U16,
    #     write_method = WRITE_DATA_LOCAL,
    # ),
    # SofarModbusNumberEntityDescription(
    #     name = "TOU: Target SOC",
    #     key = "tou_target_soc",
    #     allowedtypes = HYBRID,
    #     native_min_value = 30,
    #     native_max_value = 100,
    #     native_unit_of_measurement = PERCENTAGE,
    #     initvalue = 0,
    #     prevent_update = True,
    #     unit = REGISTER_U16,
    #     write_method = WRITE_DATA_LOCAL,
    # ),
    # SofarModbusNumberEntityDescription(
    #     name = "TOU: Charge Power",
    #     key = "tou_charge_power",
    #     allowedtypes = HYBRID,
    #     native_min_value = 0,
    #     native_max_value = 6000,
    #     native_step = 100,
    #     native_unit_of_measurement = UnitOfPower.WATT,
    #     initvalue = 0,
    #     prevent_update = True,
    #     unit = REGISTER_U32,
    #     write_method = WRITE_DATA_LOCAL,
    # ),
    ###
    #
    #  Normal number types
    #
    ###
    SofarModbusNumberEntityDescription(
        name="Parallel Address",
        key="parallel_address",
        register=0x1037,
        fmt="i",
        native_min_value=0,
        native_max_value=10,
        native_step=1,
        allowedtypes=HYBRID | PV | X3 | PM,
        write_method=WRITE_MULTISINGLE_MODBUS,
        entity_category=EntityCategory.CONFIG,
    ),
]

# ================================= Select Declarations ============================================================

SELECT_TYPES = [
    ###
    #
    #  Data only select types
    #
    ###
    SofarModbusSelectEntityDescription(
        name="FeedIn: Limitation Mode",
        key="feedin_limitation_mode",
        register_data_type=REGISTER_U16,
        option_dict={
            0: "Disabled",
            1: "Enabled - Feed-in limitation",
            2: "Enabled - 3-phase limit",
        },
        allowedtypes=HYBRID | PV | X3,
        write_method=WRITE_DATA_LOCAL,
        icon="mdi:transmission-tower-import",
    ),
    SofarModbusSelectEntityDescription(
        name="FeedIn: Limitation Mode",
        key="feedin_limitation_mode",
        register_data_type=REGISTER_U16,
        option_dict={
            0: "Disabled",
            1: "Enabled - Feed-in limitation",
        },
        allowedtypes=HYBRID | PV | X1,
        write_method=WRITE_DATA_LOCAL,
        icon="mdi:transmission-tower-import",
    ),
    # TIMING AND TOU DISABLED AS THESE ARE NOT WORKING
    # SofarModbusSelectEntityDescription(
    #     name = "Timing: Charge",
    #     key = "timing_charge",
    #     unit = REGISTER_U16,
    #     write_method = WRITE_DATA_LOCAL,
    #     option_dict =  {
    #             0: "Enabled - Charging & Discharging",
    #             1: "Enabled - Charging ",
    #             2: "Enabled - Discharging",
    #             3: "Disabled",
    #         },
    #     allowedtypes = HYBRID,
    # ),
    # SofarModbusSelectEntityDescription(
    #     name = "Timing: Charge Start Time",
    #     key = "timing_charge_start_time",
    #     unit = REGISTER_U16,
    #     write_method = WRITE_DATA_LOCAL,
    #     option_dict = TIME_OPTIONS_GEN4,
    #     allowedtypes = HYBRID,
    #     entity_category = EntityCategory.CONFIG,
    #     icon = "mdi:battery-clock",
    # ),
    # SofarModbusSelectEntityDescription(
    #     name = "Timing: Charge End Time",
    #     key = "timing_charge_end_time",
    #     unit = REGISTER_U16,
    #     write_method = WRITE_DATA_LOCAL,
    #     option_dict = TIME_OPTIONS_GEN4,
    #     allowedtypes = HYBRID,
    #     entity_category = EntityCategory.CONFIG,
    #     icon = "mdi:battery-clock",
    # ),
    # SofarModbusSelectEntityDescription(
    #     name = "Timing: Discharge Start Time",
    #     key = "timing_discharge_start_time",
    #     unit = REGISTER_U16,
    #     write_method = WRITE_DATA_LOCAL,
    #     option_dict = TIME_OPTIONS_GEN4,
    #     allowedtypes = HYBRID,
    #     entity_category = EntityCategory.CONFIG,
    #     icon = "mdi:battery-clock",
    # ),
    # SofarModbusSelectEntityDescription(
    #     name = "Timing: Discharge End Time",
    #     key = "timing_discharge_end_time",
    #     unit = REGISTER_U16,
    #     write_method = WRITE_DATA_LOCAL,
    #     option_dict = TIME_OPTIONS_GEN4,
    #     allowedtypes = HYBRID,
    #     entity_category = EntityCategory.CONFIG,
    #     icon = "mdi:battery-clock",
    # ),
    # SofarModbusSelectEntityDescription(
    #     name = "TOU: Control",
    #     key = "tou_control",
    #     unit = REGISTER_U16,
    #     write_method = WRITE_DATA_LOCAL,
    #     option_dict =  {
    #             0: "Enabled - Charging & Discharging",
    #             1: "Enabled - Charging ",
    #             2: "Enabled - Discharging",
    #             3: "Disabled",
    #         },
    #     allowedtypes = HYBRID,
    # ),
    # SofarModbusSelectEntityDescription(
    #     name = "TOU: Charge Start Time",
    #     key = "tou_charge_start_time",
    #     unit = REGISTER_U16,
    #     write_method = WRITE_DATA_LOCAL,
    #     option_dict = TIME_OPTIONS_GEN4,
    #     allowedtypes = HYBRID,
    #     entity_category = EntityCategory.CONFIG,
    #     icon = "mdi:battery-clock",
    # ),
    # SofarModbusSelectEntityDescription(
    #     name = "TOU: Charge End Time",
    #     key = "tou_charge_end_time",
    #     unit = REGISTER_U16,
    #     write_method = WRITE_DATA_LOCAL,
    #     option_dict = TIME_OPTIONS_GEN4,
    #     allowedtypes = HYBRID,
    #     entity_category = EntityCategory.CONFIG,
    #     icon = "mdi:battery-clock",
    # ),
    SofarModbusSelectEntityDescription(
        name="Passive: Timeout",
        key="passive_mode_timeout",
        register_data_type=REGISTER_U16,
        write_method=WRITE_DATA_LOCAL,
        option_dict={
            0: "Disabled",
            300: "5 Minutes",
            600: "10 Minutes",
            900: "15 Minutes",
            1800: "30 Minutes",
            3600: "60 Minutes",
            5400: "90 Minutes",
            7200: "2 Hours",
            10800: "3 Hours",
            14400: "4 Hours",
            18000: "5 Hours",
            21600: "6 Hours",
        },
        allowedtypes=HYBRID,
        icon="mdi:timer",
    ),
    SofarModbusSelectEntityDescription(
        name="Passive: Timeout Action",
        key="passive_mode_timeout_action",
        register_data_type=REGISTER_U16,
        write_method=WRITE_DATA_LOCAL,
        option_dict={
            0: "Force Standby",
            1: "Return to Previous Mode",
        },
        allowedtypes=HYBRID,
        icon="mdi:timer-cog",
    ),
    ###
    #
    #  Normal select types
    #
    ###
    SofarModbusSelectEntityDescription(
        name="EPS Mode",
        key="eps_control",
        register_data_type=REGISTER_U16,
        write_method=WRITE_DATA_LOCAL,
        option_dict={
            0: "Turn Off",
            1: "Turn On, Prohibit Cold Start",
            2: "Turn On, Enable Cold Start",
        },
        allowedtypes=HYBRID | EPS,
        icon="mdi:power-plug-off",
    ),
    # Does not work. 0x1035, 0x1036, and 0x1037 have to be written in one single chunk
    # SofarModbusSelectEntityDescription(
    #     name = "Parallel Control",
    #     key = "parallel_control",
    #     register = 0x1035,
    #     option_dict =  {
    #             0: "Disabled",
    #             1: "Enabled",
    #         },
    #     allowedtypes = HYBRID | PV | X3 | PM,
    #     write_method = WRITE_MULTISINGLE_MODBUS,
    # ),
    # SofarModbusSelectEntityDescription(
    #     name = "Parallel Master-Salve",
    #     key = "parallel_masterslave",
    #     register = 0x1036,
    #     option_dict =  {
    #             0: "Slave",
    #             1: "Master",
    #         },
    #     allowedtypes = HYBRID | PV | X3 | PM,
    #     write_method = WRITE_MULTISINGLE_MODBUS,
    # ),
    SofarModbusSelectEntityDescription(
        name="Remote Switch On Off",
        key="remote_switch_on_off",
        register=0x1104,
        option_dict={
            0: "Off",
            1: "On",
        },
        allowedtypes=HYBRID | PV,
        write_method=WRITE_MULTISINGLE_MODBUS,
    ),
    SofarModbusSelectEntityDescription(
        name="Energy Storage Mode",
        key="charger_use_mode",
        register=0x1110,
        option_dict={
            0: "Self Use",
            1: "Time of Use",
            2: "Timing Mode",
            3: "Passive Mode",
            4: "Peak Cut Mode",
            5: "Off-grid Mode",
            6: "Generator mode",
            7: "Feed-In Priority Mode",
        },
        allowedtypes=HYBRID | X3,
        write_method=WRITE_MULTISINGLE_MODBUS,
        icon="mdi:battery-charging-60",
    ),
    SofarModbusSelectEntityDescription(
        name="Energy Storage Mode",
        key="charger_use_mode",
        register=0x1110,
        option_dict={
            0: "Self Use",
            1: "Time of Use",
            2: "Timing Mode",
            3: "Passive Mode",
            4: "Peak Cut Mode",
            5: "Off-grid Mode",
        },
        allowedtypes=HYBRID | X1,
        write_method=WRITE_MULTISINGLE_MODBUS,
        icon="mdi:battery-charging-60",
    ),
    # Timing Charge Start
    # Timing Charge End
    # Timing Discharge Start
    # Timing Discharge End
    # TOU Charge Start
    # TOU Charge End
]


# ================================= Sensor Declarations ============================================================

SENSOR_TYPES: list[SofarModbusSensorEntityDescription] = [
    ###
    #
    # Real-time Data Area
    #
    ###
    SofarModbusSensorEntityDescription(
        name="System State",
        key="system_state",
        register=0x404,
        newblock=True,
        scale={
            0: "Waiting",
            1: "Checking",
            2: "Grid-connected",
            3: "Emergency Power Supply",
            4: "Recoverable fault",
            5: "Permanent fault",
            6: "Upgrading",
            7: "Self-Charging",
        },
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 1",
        key="fault_1",
        register=0x405,
        scale={
            0: "No error",
            1: "ID01 Grid Over Voltage Protection",
            2: "ID02 Grid Under Voltage Protection",
            4: "ID03 Grid Over Frequency Protection",
            8: "ID04 Grid Under Frequency Protection",
            16: "ID05 Leakage current fault",
            32: "ID06 High penetration error",
            64: "ID07 Low penetration error",
            128: "ID08 Islanding error",
            256: "ID09 Grid voltage transient value overvoltage 1",
            512: "ID10 Grid voltage transient value overvoltage 2",
            1024: "ID11 Grid line voltage error",
            2048: "ID12 Inverter voltage error",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 2",
        key="fault_2",
        register=0x406,
        scale={
            0: "No error",
            1: "ID17 Grid current sampling error",
            2: "ID18 Grid current DC component sampling error (AC side)",
            4: "ID19 Grid voltage sampling error (DC side)",
            8: "ID20 Grid voltage sampling error (AC side)",
            16: "ID21 Leakage current sampling error (DC side)",
            32: "ID22 Leakage current sampling error (AC side)",
            64: "ID23 Load voltage DC component sampling error",
            128: "ID24 DC input current sampling error",
            256: "ID25 DC component sampling error of grid current (DC side)",
            512: "ID26 DC input branch current sampling error",
            4096: "ID29 Leakage current consistency error",
            8192: "ID30 Grid voltage consistency error",
            16384: "ID31 DCI consistency error",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 3",
        key="fault_3",
        register=0x407,
        scale={
            0: "No error",
            1: "ID033 SPI communication error (DC side)",
            2: "ID034 SPI communication error (AC side)",
            4: "ID035 Chip error (DC side)",
            8: "ID036 Chip error (AC side)",
            16: "ID037 Auxiliary power error",
            32: "ID038 Inverter soft start failure",
            256: "ID041 Relay detection failure",
            512: "ID042 Low insulation impedance",
            1024: "ID043 Grounding error",
            2048: "ID044 Input mode setting error",
            4096: "ID045 ID045 CT error",
            8192: "ID046 Input reversal error",
            16384: "ID047 Parallel error",
            32768: "ID048 Serial number error",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 4",
        key="fault_4",
        register=0x408,
        scale={
            0: "No error",
            1: "ID049 Battery temperature protection",
            2: "ID050 Heat sink 1 temperature protection",
            4: "ID051 Heater 2 temperature protection",
            8: "ID052 Heater 3 temperature protection",
            16: "ID053 Heatsink 4 temperature protection",
            32: "ID054 Heatsink 5 temperature protection",
            64: "ID055 Radiator 6 temperature protection",
            256: "ID057 Ambient temperature 1 protection",
            512: "ID058 Ambient temperature 2 protection",
            1024: "ID059 Module 1 temperature protection",
            2048: "ID060 Module 2 temperature protection",
            4096: "ID061 Module 3 temperature protection",
            8192: "ID062 Module temperature difference is too large",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 5",
        key="fault_5",
        register=0x409,
        scale={
            0: "No error",
            1: "ID065 Bus voltage RMS unbalance",
            2: "ID066 Bus voltage transient value unbalance",
            4: "ID067 Undervoltage of busbar during grid connection",
            8: "ID068 Bus bar low voltage",
            16: "ID069 PV overvoltage",
            32: "ID070 Battery over-voltage",
            64: "ID071 LLCBus overvoltage protection",
            128: "ID072 Inverter bus voltage RMS software overvoltage",
            256: "ID073 Inverter bus voltage transient value software overvoltage",
            512: "ID074 Flying Cross Capacitor Overvoltage Protection",
            1024: "ID075 Flying Cross capacitor undervoltage protection",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 6",
        key="fault_6",
        register=0x040A,
        scale={
            0: "No error",
            1: "ID081 Battery overcurrent software protection",
            2: "ID082 Dci overcurrent protection",
            4: "ID083 Output transient current protection",
            8: "ID084 BuckBoost software overcurrent",
            16: "ID085 Output RMS current protection",
            32: "ID086 PV instantaneous current overcurrent software protection",
            64: "ID087 PV parallel uneven current",
            128: "ID088 Output current unbalance",
            256: "ID089 PV software overcurrent protection",
            512: "ID090 Balanced circuit overcurrent protection",
            1024: "ID091 Resonance protection",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 7",
        key="fault_7",
        register=0x040B,
        scale={
            0: "No error",
            1: "ID097 LLC bus hardware overvoltage",
            2: "ID098 Inverter bus hardware overvoltage",
            4: "ID099 BuckBoost hardware overcurrent",
            8: "ID100 Battery hardware overcurrent",
            16: "ID101",
            32: "ID102 PV hardware overcurrent",
            64: "ID103 AC output hardware overcurrent",
            256: "ID105 Power meter error",
            512: "ID106 Serial number model error",
            8192: "ID110 Overload protection 1",
            16384: "ID111 Overload protection 2",
            32768: "ID112 Overload protection 3",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 8",
        key="fault_8",
        register=0x040C,
        scale={
            0: "No error",
            1: "ID113 Overtemperature derating",
            2: "ID114 Frequency down load",
            4: "ID115 Frequency loading",
            8: "ID116 Voltage down load",
            16: "ID117 Voltage loading",
            256: "ID121 Lightning protection failure (DC)",
            512: "ID122 Lightning protection failure (AC)",
            2048: "ID124 Battery low voltage protection",
            4096: "ID125 Battery low voltage shutdown",
            8192: "ID126 Battery low voltage pre-alarm",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 9",
        key="fault_9",
        register=0x040D,
        scale={
            0: "No error",
            1: "ID129 Output hardware overcurrent permanent fault",
            2: "ID130 Bus overvoltage permanent fault",
            4: "ID131 Bus hardware over-voltage permanent fault",
            8: "ID132 PV uneven flow permanent fault",
            16: "ID133 Battery overcurrent permanent fault in EPS mode",
            32: "ID134 Output transient overcurrent permanent fault",
            64: "ID135 Output current unbalance permanent fault",
            256: "ID137 Input mode setting error permanent fault",
            512: "ID138 Input overcurrent permanent fault",
            1024: "ID139 Input hardware overcurrent permanent fault",
            2048: "ID140 Relay permanent fault",
            4096: "ID141 Bus unbalance permanent fault",
            8192: "ID142 Lightning protection permanent fault - DC side",
            16384: "ID143 Lightning protection permanent fault - AC side",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 10",
        key="fault_10",
        register=0x040E,
        scale={
            0: "No error",
            1: "ID145 USB fault",
            2: "ID146 WIFI fault",
            4: "ID147 Bluetooth fault",
            8: "ID148 RTC clock fault",
            16: "ID149 Communication board EEPROM error",
            32: "ID150 Communication board FLASH error",
            128: "ID152 Safety regulation version error",
            256: "ID153 SCI communication error (DC side)",
            512: "ID154 SCI communication error (AC side)",
            1024: "ID155 SCI communication error (convergence board side)",
            2048: "ID156 Software version inconsistency",
            4096: "ID157 Lithium battery 1 communication error",
            8192: "ID158 Li-ion battery 2 communication error",
            16384: "ID159 Lithium battery 3 communication error",
            32768: "ID160 Lithium battery 4 communication failure",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 11",
        key="fault_11",
        register=0x040F,
        scale={
            0: "No error",
            1: "ID161 Forced shutdown",
            2: "ID162 Remote shutdown",
            4: "ID163 Drms0 shutdown",
            16: "ID165 Remote down load",
            32: "ID166 Logic interface down load",
            64: "ID167 Anti-Reverse Flow Downgrade",
            256: "ID169 Fan 1 failure",
            512: "ID170 Fan 2 failure",
            1024: "ID171 Fan 3 failure",
            2048: "ID172 Fan 4 failure",
            4096: "ID173 Fan 5 failure",
            8192: "ID174 Fan 6 failure",
            16384: "ID175 Fan 7 fault",
            32768: "ID176 Meter communication failure",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault 12",
        key="fault_12",
        register=0x0410,
        scale={
            0: "No error",
            1: "ID177 BMS over-voltage alarm",
            2: "ID178 BMS undervoltage alarm",
            4: "ID179 BMS high temperature alarm",
            8: "ID180 BMS low temperature alarm",
            16: "ID181 BMS charge/discharge overload alarm",
            32: "ID182 BMS short circuit alarm",
            64: "ID183 BMS version inconsistency",
            128: "ID184 BMS CAN version inconsistency",
            256: "ID185 BMS CAN version is too low",
            4096: "ID189 Arc device communication failure",
            8192: "ID190 DC arc alarm fault",
            16384: "ID191 PID repair failed",
            32768: "ID192 PLC module heartbeat loss",
        },
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Waiting Time",
        key="waiting_time",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        register=0x417,
        register_data_type=REGISTER_S16,
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Inverter Temperature 1",
        key="inverter_temperature_1",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x418,
        register_data_type=REGISTER_S16,
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Inverter Temperature 2",
        key="inverter_temperature_2",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x419,
        register_data_type=REGISTER_S16,
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Heatsink Temperature 1",
        key="heatsink_temperature_1",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x41A,
        # newblock = True,
        register_data_type=REGISTER_S16,
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Heatsink Temperature 2",
        key="heatsink_temperature_2",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x41B,
        register_data_type=REGISTER_S16,
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Module Temperature 1",
        key="module_temperature_1",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x420,
        register_data_type=REGISTER_S16,
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Module Temperature 2",
        key="module_temperature_2",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x421,
        register_data_type=REGISTER_S16,
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="System Time",
        key="rtc",
        register=0x42C,
        register_data_type=REGISTER_WORDS,
        wordcount=6,
        scale=value_function_rtc_ymd,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock",
    ),
    SofarModbusSensorEntityDescription(
        name="Serial Number",
        key="serial_number",
        register=0x445,
        # newblock=True,
        register_data_type=REGISTER_STR,
        wordcount=7,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Hardware Version",
        key="hardware_version",
        register=0x44D,
        register_data_type=REGISTER_STR,
        # newblock=True, # due to problems reported by some users
        entity_registry_enabled_default=False,  # causing problems for some users
        wordcount=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Software Version",
        key="software_version",
        register=0x44F,
        register_data_type=REGISTER_STR,
        wordcount=4,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=HYBRID | PV,
    ),
    ###
    #
    # On Grid Output
    #
    ###
    SofarModbusSensorEntityDescription(
        name="Grid Frequency",
        key="grid_frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        register=0x484,
        newblock=True,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power Output Total",
        key="active_power_output_total",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x485,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Reactive Power Output Total",
        key="reactive Power_output_total",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x486,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Apparent Power Output Total",
        key="apparent_power_output_total",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x487,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power PCC Total",
        key="active_power_pcc_total",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x488,
        register_data_type=REGISTER_S16,
        scan_group=SCAN_GROUP_FAST,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Reactive Power PCC Total",
        key="reactive Power_pcc_total",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x489,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Apparent Power PCC Total",
        key="apparent_power_pcc_total",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x48A,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Voltage L1",
        key="voltage_l1",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x48D,
        scan_group=SCAN_GROUP_FAST,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Current Output L1",
        key="current_output_l1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x48E,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power Output L1",
        key="active_power_output_l1",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x48F,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Reactive Power Output L1",
        key="reactive Power_output_l1",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        register=0x490,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Power Factor Output L1",
        key="power_factor_output_l1",
        device_class=SensorDeviceClass.POWER_FACTOR,
        native_unit_of_measurement=None,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x491,
        register_data_type=REGISTER_S16,
        scale=0.001,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Current PCC L1",
        key="current_pcc_l1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x492,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power PCC L1",
        key="active_power_pcc_l1",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x493,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Reactive Power PCC L1",
        key="reactive Power_pcc_l1",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        register=0x494,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Power Factor PCC L1",
        key="power_factor_pcc_l1",
        device_class=SensorDeviceClass.POWER_FACTOR,
        native_unit_of_measurement=None,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x495,
        register_data_type=REGISTER_S16,
        scale=0.001,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Voltage L2",
        key="voltage_l2",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x498,
        scan_group=SCAN_GROUP_FAST,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Current Output L2",
        key="current_output_l2",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x499,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power Output L2",
        key="active_power_output_l2",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x49A,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Reactive Power Output L2",
        key="reactive Power_output_l2",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        register=0x49B,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Power Factor Output L2",
        key="power_factor_output_l2",
        device_class=SensorDeviceClass.POWER_FACTOR,
        native_unit_of_measurement=None,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x49C,
        register_data_type=REGISTER_S16,
        scale=0.001,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Current PCC L2",
        key="current_pcc_l2",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x49D,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power PCC L2",
        key="active_power_pcc_l2",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x49E,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Reactive Power PCC L2",
        key="reactive Power_pcc_l2",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        register=0x49F,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Power Factor PCC L2",
        key="power_factor_pcc_l2",
        device_class=SensorDeviceClass.POWER_FACTOR,
        native_unit_of_measurement=None,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x4A0,
        register_data_type=REGISTER_S16,
        scale=0.001,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Voltage L3",
        key="voltage_l3",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x4A3,
        scan_group=SCAN_GROUP_FAST,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Current Output L3",
        key="current_output_l3",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x4A4,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power Output L3",
        key="active_power_output_l3",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x4A5,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Reactive Power Output L3",
        key="reactive Power_output_l3",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        register=0x4A6,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Power Factor Output L3",
        key="power_factor_output_l3",
        device_class=SensorDeviceClass.POWER_FACTOR,
        native_unit_of_measurement=None,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x4A7,
        register_data_type=REGISTER_S16,
        scale=0.001,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Current PCC L3",
        key="current_pcc_l3",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x4A8,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power PCC L3",
        key="active_power_pcc_l3",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x4A9,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Reactive Power PCC L3",
        key="reactive Power_pcc_l3",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        register=0x4AA,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Power Factor PCC L3",
        key="power_factor_pcc_l3",
        device_class=SensorDeviceClass.POWER_FACTOR,
        native_unit_of_measurement=None,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x4AB,
        register_data_type=REGISTER_S16,
        scale=0.001,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power PV Ext",
        key="active_power_pv_ext",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x4AE,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power Load Sys",
        key="active_power_load_sys",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x4AF,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Voltage Phase L1N",
        key="voltage_phase_l1n",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x4B0,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Current Output L1N",
        key="current_output_l1n",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x4B1,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power Output L1N",
        key="active_power_output_l1n",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x4B2,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Current PCC L1N",
        key="current_pcc_l1n",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x4B3,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power PCC L1N",
        key="active_power_pcc_l1n",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x4B4,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Voltage Phase L2N",
        key="voltage_phase_l2n",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x4B5,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Current Output L2N",
        key="current_output_l2n",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x4B6,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power Output L2N",
        key="active_power_output_l2n",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x4B7,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Current PCC L2N",
        key="current_pcc_l2n",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x4B8,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Active Power PCC L2N",
        key="active_power_pcc_l2n",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x4B9,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Voltage Line L1",
        key="voltage_line_l1",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x4BA,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Voltage Line L2",
        key="voltage_line_l2",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x4BB,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Voltage Line L3",
        key="voltage_line_l3",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x4BC,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
    ),
    ###
    #
    # Off Grid Output (0x0500-0x057F)
    #
    ###
    SofarModbusSensorEntityDescription(
        name="Active Power Off-Grid Total",
        key="active_power_offgrid_total",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x504,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Reactive Power Off-Grid Total",
        key="reactive Power_offgrid_total",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        register=0x505,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Apparent Power Off-Grid Total",
        key="apparent_power_offgrid_total",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x506,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Frequency",
        key="offgrid_frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        register=0x507,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Voltage",
        key="offgrid_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x50A,
        scan_group=SCAN_GROUP_FAST,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | X1 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Voltage L1",
        key="offgrid_voltage_l1",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x50A,
        scan_group=SCAN_GROUP_FAST,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Current Output",
        key="offgrid_current_output",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x50B,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X1 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Current Output L1",
        key="offgrid_current_output_l1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x50B,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Active Power Output",
        key="offgrid_active_power_output",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x50C,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X1 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Active Power Output L1",
        key="offgrid_active_power_output_l1",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x50C,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Reactive Power Output",
        key="offgrid_reactive Power_output",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        register=0x50D,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | X1 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Reactive Power Output L1",
        key="offgrid_reactive Power_output_l1",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        register=0x50D,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Apparent Power Output",
        key="offgrid_apparent_power_output",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        register=0x50E,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X1 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Apparent Power Output L1",
        key="offgrid_apparent_power_output_l1",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        register=0x50E,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid LoadPeakRatio",
        key="offgrid_loadpeakratio",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        register=0x50F,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X1 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid LoadPeakRatio L1",
        key="offgrid_loadpeakratio_l1",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        register=0x50F,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Voltage L2",
        key="offgrid_voltage_l2",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x512,
        scan_group=SCAN_GROUP_FAST,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Current Output L2",
        key="offgrid_current_output_l2",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x513,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Active Power Output L2",
        key="offgrid_active_power_output_l2",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x514,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Reactive Power Output L2",
        key="offgrid_reactive Power_output_l2",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        register=0x515,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Apparent Power Output L2",
        key="offgrid_apparent_power_output_l2",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        register=0x516,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid LoadPeakRatio L2",
        key="offgrid_loadpeakratio_l2",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        register=0x517,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Voltage L3",
        key="offgrid_voltage_l3",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x51A,
        scan_group=SCAN_GROUP_FAST,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Current Output L3",
        key="offgrid_current_output_l3",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x51B,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Active Power Output L3",
        key="offgrid_active_power_output_l3",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x51C,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Reactive Power Output L3",
        key="offgrid_reactive Power_output_l3",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        register=0x51D,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Apparent Power Output L3",
        key="offgrid_apparent_power_output_l3",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        register=0x51E,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid LoadPeakRatio L3",
        key="offgrid_loadpeakratio_l3",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        register=0x51F,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Voltage Output L1N",
        key="offgrid_voltage_output_l1n",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x522,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Current Output L1N",
        key="offgrid_current_output_l1n",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x523,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Active Power Output L1N",
        key="offgrid_active_power_output_l1n",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x524,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Voltage Output L2N",
        key="offgrid_voltage_output_l2n",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x525,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Current Output L2N",
        key="offgrid_current_output_l2n",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x526,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    SofarModbusSensorEntityDescription(
        name="Off-Grid Active Power Output L2N",
        key="offgrid_active_power_output_l2n",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x527,
        newblock=True,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | X3 | EPS,
    ),
    ###
    #
    # PV Input (0x0580-0x05FF)
    #
    ###
    SofarModbusSensorEntityDescription(
        name="PV Voltage 1",
        key="pv_voltage_1",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x584,
        newblock=True,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Current 1",
        key="pv_current_1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x585,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Power 1",
        key="pv_power_1",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x586,
        scan_group=SCAN_GROUP_FAST,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Voltage 2",
        key="pv_voltage_2",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x587,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Current 2",
        key="pv_current_2",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x588,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Power 2",
        key="pv_power_2",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x589,
        scan_group=SCAN_GROUP_FAST,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Voltage 3",
        key="pv_voltage_3",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x58A,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV | GEN | ALL_MPPT_GROUP,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Current 3",
        key="pv_current_3",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x58B,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | ALL_MPPT_GROUP,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Power 3",
        key="pv_power_3",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x58C,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | ALL_MPPT_GROUP,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Voltage 4",
        key="pv_voltage_4",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x58D,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV | GEN | MPPT4 | MPPT6 | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Current 4",
        key="pv_current_4",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x58E,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT4 | MPPT6 | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Power 4",
        key="pv_power_4",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x58F,
        newblock=True,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT4 | MPPT6 | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Voltage 5",
        key="pv_voltage_5",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x590,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV | GEN | MPPT6 | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Current 5",
        key="pv_current_5",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x591,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT6 | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Power 5",
        key="pv_power_5",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x592,
        newblock=True,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT6 | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Voltage 6",
        key="pv_voltage_6",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x593,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV | GEN | MPPT6 | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Current 6",
        key="pv_current_6",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x594,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT6 | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Power 6",
        key="pv_power_6",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x594,
        newblock=True,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT6 | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Voltage 7",
        key="pv_voltage_7",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x596,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV | GEN | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Current 7",
        key="pv_current_7",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x597,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Power 7",
        key="pv_power_7",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x598,
        newblock=True,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Voltage 8",
        key="pv_voltage_8",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x599,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV | GEN | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Current 8",
        key="pv_current_8",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x59A,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Power 8",
        key="pv_power_8",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x59B,
        newblock=True,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT8 | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Voltage 9",
        key="pv_voltage_9",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x59C,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV | GEN | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Current 9",
        key="pv_current_9",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x59D,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Power 9",
        key="pv_power_9",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x59E,
        newblock=True,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Voltage 10",
        key="pv_voltage_10",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x59F,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV | GEN | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Current 10",
        key="pv_current_10",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x5A0,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Power 10",
        key="pv_power_10",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x5A1,
        newblock=True,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV | GEN | MPPT10,
    ),
    SofarModbusSensorEntityDescription(
        name="PV Power Total",
        key="pv_power_total",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x5C4,
        newblock=True,
        scale=0.1,
        rounding=1,
        # entity_registry_enabled_default =  False,
        allowedtypes=HYBRID | PV | GEN,
    ),
    ###
    #
    # Battery Input (0x0600-0x067F)
    #
    ###
    SofarModbusSensorEntityDescription(
        name="Battery Voltage 1",
        key="battery_voltage_1",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x604,
        newblock=True,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Current 1",
        key="battery_current_1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x605,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Power 1",
        key="battery_power_1",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x606,
        register_data_type=REGISTER_S16,
        scan_group=SCAN_GROUP_FAST,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Temperature 1",
        key="battery_temperature_1",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x607,
        register_data_type=REGISTER_S16,
        allowedtypes=HYBRID,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Capacity 1",
        key="battery_capacity_1",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        # device_class = SensorDeviceClass.BATTERY,
        register=0x608,
        allowedtypes=HYBRID,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery State of Health 1",
        key="battery_state_of_health_1",
        register=0x609,
        native_unit_of_measurement=PERCENTAGE,
        allowedtypes=HYBRID,
        icon="mdi:battery-heart",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Charge Cycle 1",
        key="battery_charge_cycle_1",
        state_class=SensorStateClass.MEASUREMENT,
        register=0x60A,
        allowedtypes=HYBRID,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Voltage 2",
        key="battery_voltage_2",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x60B,
        newblock=True,
        scale=0.1,
        rounding=1,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Current 2",
        key="battery_current_2",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x60C,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Power 2",
        key="battery_power_2",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x60D,
        register_data_type=REGISTER_S16,
        scan_group=SCAN_GROUP_FAST,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Temperature 2",
        key="battery_temperature_2",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x60E,
        register_data_type=REGISTER_S16,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Capacity 2",
        key="battery_capacity_2",
        native_unit_of_measurement=PERCENTAGE,
        # device_class = SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x60F,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery State of Health 2",
        key="battery_state_of_health_2",
        register=0x610,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-heart",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Charge Cycle 2",
        key="battery_charge_cycle_2",
        state_class=SensorStateClass.MEASUREMENT,
        register=0x611,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Voltage 3",
        key="battery_voltage_3",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x612,
        newblock=True,
        scale=0.1,
        rounding=1,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Current 3",
        key="battery_current_3",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x613,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Power 3",
        key="battery_power_3",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x614,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Temperature 3",
        key="battery_temperature_3",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x615,
        register_data_type=REGISTER_S16,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Capacity 3",
        key="battery_capacity_3",
        native_unit_of_measurement=PERCENTAGE,
        # device_class = SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x616,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery State of Health 3",
        key="battery_state_of_health_3",
        register=0x617,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        icon="mdi:battery-heart",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Charge Cycle 3",
        key="battery_charge_cycle_3",
        state_class=SensorStateClass.MEASUREMENT,
        register=0x618,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Voltage 4",
        key="battery_voltage_4",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x619,
        newblock=True,
        scale=0.1,
        rounding=1,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Current 4",
        key="battery_current_4",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x61A,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Power 4",
        key="battery_power_4",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x61B,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Temperature 4",
        key="battery_temperature_4",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x61C,
        register_data_type=REGISTER_S16,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Capacity 4",
        key="battery_capacity_4",
        native_unit_of_measurement=PERCENTAGE,
        # device_class = SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x61D,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery State of Health 4",
        key="battery_state_of_health_4",
        register=0x61E,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        icon="mdi:battery-heart",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Charge Cycle 4",
        key="battery_charge_cycle_4",
        state_class=SensorStateClass.MEASUREMENT,
        register=0x61F,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Voltage 5",
        key="battery_voltage_5",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x620,
        newblock=True,
        scale=0.1,
        rounding=1,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Current 5",
        key="battery_current_5",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x621,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Power 5",
        key="battery_power_5",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x622,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Temperature 5",
        key="battery_temperature_5",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x623,
        register_data_type=REGISTER_S16,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Capacity 5",
        key="battery_capacity_5",
        native_unit_of_measurement=PERCENTAGE,
        # device_class = SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x624,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery State of Health 5",
        key="battery_state_of_health_5",
        register=0x625,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        icon="mdi:battery-heart",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Charge Cycle 5",
        key="battery_charge_cycle_5",
        state_class=SensorStateClass.MEASUREMENT,
        register=0x626,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Voltage 6",
        key="battery_voltage_6",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x627,
        newblock=True,
        scale=0.1,
        rounding=1,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Current 6",
        key="battery_current_6",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x628,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Power 6",
        key="battery_power_6",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x629,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Temperature 6",
        key="battery_temperature_6",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x62A,
        register_data_type=REGISTER_S16,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Capacity 6",
        key="battery_capacity_6",
        native_unit_of_measurement=PERCENTAGE,
        # device_class = SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x62B,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery State of Health 6",
        key="battery_state_of_health_6",
        register=0x62C,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        icon="mdi:battery-heart",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Charge Cycle 6",
        key="battery_charge_cycle_6",
        state_class=SensorStateClass.MEASUREMENT,
        register=0x62D,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Voltage 7",
        key="battery_voltage_7",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x62E,
        newblock=True,
        scale=0.1,
        rounding=1,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Current 7",
        key="battery_current_7",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x62F,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Power 7",
        key="battery_power_7",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x630,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Temperature 7",
        key="battery_temperature_7",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x631,
        register_data_type=REGISTER_S16,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Capacity 7",
        key="battery_capacity_7",
        native_unit_of_measurement=PERCENTAGE,
        # device_class = SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x632,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery State of Health 7",
        key="battery_state_of_health_7",
        register=0x633,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        icon="mdi:battery-heart",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Charge Cycle 7",
        key="battery_charge_cycle_7",
        state_class=SensorStateClass.MEASUREMENT,
        register=0x634,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Voltage 8",
        key="battery_voltage_8",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x635,
        newblock=True,
        scale=0.1,
        rounding=1,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Current 8",
        key="battery_current_8",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x636,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Power 8",
        key="battery_power_8",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x637,
        register_data_type=REGISTER_S16,
        scale=0.01,
        rounding=2,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Temperature 8",
        key="battery_temperature_8",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x638,
        register_data_type=REGISTER_S16,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Capacity 8",
        key="battery_capacity_8",
        native_unit_of_measurement=PERCENTAGE,
        # device_class = SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x639,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery State of Health 8",
        key="battery_state_of_health_8",
        register=0x63A,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        icon="mdi:battery-heart",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Charge Cycle 8",
        key="battery_charge_cycle_8",
        state_class=SensorStateClass.MEASUREMENT,
        register=0x63B,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | GEN,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Power Total",
        key="battery_power_total",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x667,
        newblock=True,
        register_data_type=REGISTER_S16,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Capacity Total",
        key="battery_capacity_total",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x668,
        allowedtypes=HYBRID,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery State of Health Total",
        key="battery_state_of_health_total",
        register=0x669,
        native_unit_of_measurement=PERCENTAGE,
        allowedtypes=HYBRID,
        icon="mdi:battery-heart",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ###
    #
    # Electric Power (0x0680-0x06BF)
    #
    ###
    SofarModbusSensorEntityDescription(
        name="Solar Generation Today",
        key="solar_generation_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x684,
        ignore_readerror=True,
        newblock=True,
        register_data_type=REGISTER_U32,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Solar Generation Total",
        key="solar_generation_total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x686,
        register_data_type=REGISTER_U32,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Load Consumption Today",
        key="load_consumption_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x688,
        register_data_type=REGISTER_U32,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Load Consumption Total",
        key="load_consumption_total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x68A,
        register_data_type=REGISTER_U32,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Import Energy Today",
        key="import_energy_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x68C,
        register_data_type=REGISTER_U32,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
        icon="mdi:home-import-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="Import Energy Total",
        key="import_energy_total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x68E,
        register_data_type=REGISTER_U32,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
        icon="mdi:home-import-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="Export Energy Today",
        key="export_energy_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x690,
        register_data_type=REGISTER_U32,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID | PV,
        icon="mdi:home-export-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="Export Energy Total",
        key="export_energy_total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x692,
        register_data_type=REGISTER_U32,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID | PV,
        icon="mdi:home-export-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Input Energy Today",
        key="battery_input_energy_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x694,
        register_data_type=REGISTER_U32,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID,
        icon="mdi:battery-arrow-up",
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Input Energy Total",
        key="battery_input_energy_total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x696,
        register_data_type=REGISTER_U32,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID,
        icon="mdi:battery-arrow-up",
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Output Energy Today",
        key="battery_output_energy_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x698,
        register_data_type=REGISTER_U32,
        scale=0.01,
        rounding=2,
        allowedtypes=HYBRID,
        icon="mdi:battery-arrow-down",
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Output Energy Total",
        key="battery_output_energy_total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        register=0x69A,
        register_data_type=REGISTER_U32,
        scale=0.1,
        rounding=1,
        allowedtypes=HYBRID,
        icon="mdi:battery-arrow-down",
    ),
    ###
    #
    # Basic Parameter Configuration (0x1000-0x10FF)
    #
    ###
    SofarModbusSensorEntityDescription(
        name="FeedIn: Limitation Mode",
        key="feedin_limitation_mode",
        register=0x1023,
        scale={0: "Disabled", 1: "Enabled - Feed-in limitation", 2: "Enabled - 3-phase limit"},
        entity_registry_enabled_default=False,
        icon="mdi:transmission-tower-import",
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="FeedIn: Maximum Power",
        key="feedin_max_power",
        register=0x1024,
        scale=100,
        internal=True,
        icon="mdi:transmission-tower-import",
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="EPS Mode",
        key="eps_control",
        register=0x1029,
        scale={
            0: "Turn Off",
            1: "Turn On, Prohibit Cold Start",
            2: "Turn On, Enable Cold Start",
        },
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | EPS,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="EPS Wait Time",
        key="passive_eps_wait_time",
        register=0x102A,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID | EPS,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Active Control",
        key="battery_active_control",
        register=0x102B,
        scale=value_function_disabled_enabled,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
    ),
    SofarModbusSensorEntityDescription(
        name="Parallel Control",
        key="parallel_control",
        register=0x1035,
        scale=value_function_disabled_enabled,
        allowedtypes=HYBRID | PV | X3 | PM,
    ),
    SofarModbusSensorEntityDescription(
        name="Parallel Master-Salve",
        key="parallel_masterslave",
        register=0x1036,
        scale={
            0: "Slave",
            1: "Master",
        },
        allowedtypes=HYBRID | PV | X3 | PM,
    ),
    SofarModbusSensorEntityDescription(
        name="Parallel Address",
        key="parallel_address",
        register=0x1037,
        internal=True,
        allowedtypes=HYBRID | PV | X3 | PM,
    ),
    ###
    #
    # Battery Configuration (0x1044-0x105A)
    # Read-only for now. On write all 23 registers would need to be set in one chunk.
    #
    ###
    SofarModbusSensorEntityDescription(
        name="BatConfig: ID",
        key="bat_config_id",
        register=0x1044,
        newblock=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Address 1",
        key="bat_config_address_1",
        register=0x1045,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Protocol",
        key="bat_config_protocol",
        register=0x1046,
        scale={
            0: "First Flight built-in BMS/Default",
            1: "Pie Energy protocol/PYLON",
            2: "First Flight protocol/GENERAL",
            3: "AMASS",
            4: "LG",
            5: "AlphaESS",
            6: "CATL",
            7: "WECO",
            8: "Fronus",
            9: "EMS",
            10: "Nilar",
            11: "BTS 5K",
            12: "Move for",
        },
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Overvoltage Protection",
        key="bat_config_overvoltage_protection",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        register=0x1047,
        scale=0.1,
        rounding=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Charging Voltage",
        key="bat_config_charging_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        newblock=True,  # added issue #1543
        register=0x1048,
        scale=0.1,
        rounding=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Undervoltage Protection",
        key="bat_config_undervoltage_protection",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        register=0x1049,
        scale=0.1,
        rounding=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Minimum Discharge Voltage",
        key="bat_config_minimum_discharge_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        register=0x104A,
        scale=0.1,
        rounding=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Maximum Charge Current Limit",
        key="bat_config_maximum_charge_current_limit",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        register=0x104B,
        scale=0.01,
        rounding=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Maximum Discharge Current Limit",
        key="bat_config_maximum_discharge_current_limit",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        register=0x104C,
        scale=0.01,
        rounding=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Depth of Discharge",
        key="bat_config_depth_of_discharge",
        native_unit_of_measurement=PERCENTAGE,
        register=0x104D,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: End of Discharge",
        key="bat_config_end_of_discharge",
        native_unit_of_measurement=PERCENTAGE,
        register=0x104E,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Capacity",
        key="bat_config_capacity",
        native_unit_of_measurement="Ah",
        register=0x104F,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Rated Battery Voltage",
        key="bat_config_rated_battery_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        register=0x1050,
        scale=0.1,
        rounding=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Cell Type",
        key="bat_config_cell_type",
        register=0x1051,
        scale={
            0: "Lead acid",
            1: "Lithium iron phosphate",
            2: "Ternary",
            3: "Lithium titanate",
            4: "AGM",
            5: "Gel",
            6: "Flooded",
        },
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: EPS Buffer",
        key="bat_config_eps_buffer",
        native_unit_of_measurement=PERCENTAGE,
        register=0x1052,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    # 0x1053 BatConfig_Control skipped for now. Only useful when writing
    SofarModbusSensorEntityDescription(
        name="BatConfig: Address 2",
        key="bat_config_address_2",
        register=0x1054,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Address 3",
        key="bat_config_address_3",
        register=0x1055,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Address 4",
        key="bat_config_address_4",
        register=0x1056,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Lead Acid Battery Temperature Compensation Factor",
        key="bat_config_tempco",
        native_unit_of_measurement="mV/Cell",
        register=0x1057,
        register_data_type=REGISTER_S16,
        scale=0.1,
        rounding=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Lead Acid Battery Recovery Buffer",
        key="bat_config_rated_battery_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        register=0x1058,
        scale=0.1,
        rounding=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="BatConfig: Lead Acid Battery Float Voltage",
        key="bat_config_voltage_float",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        register=0x105A,
        scale=0.1,
        rounding=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        icon="mdi:battery-check-outline",
    ),
    ###
    #
    # Remote Control (0x1100-0x12FF)
    #
    ###
    SofarModbusSensorEntityDescription(
        name="Remote Switch On Off",
        key="remote_switch_on_off",
        register=0x1104,
        newblock=True,
        scale={
            0: "Off",
            1: "On",
        },
        internal=True,
        allowedtypes=HYBRID | PV,
    ),
    SofarModbusSensorEntityDescription(
        name="Charger Use Mode",
        key="charger_use_mode",
        register=0x1110,
        scale={
            0: "Self Use",
            1: "Time of Use",
            2: "Timing Mode",
            3: "Passive Mode",
            4: "Peak Cut Mode",
            5: "Off-grid Mode",
            6: "Generator mode",
            7: "Feed-In Priority Mode",
        },
        internal=True,
        allowedtypes=HYBRID,
    ),
    # TIMING AND TOU DISABLED AS THESE ARE NOT WORKING
    # SofarModbusSensorEntityDescription(
    #     name = "Timing: ID",
    #     key = "ro_timing_id",
    #     register = 0x1111,
    #     scale = { 0: "0",
    #               1: "1",
    #               2: "2",
    #               3: "3",
    #               65531: "FFFB",
    #               65532: "FFFC",
    #               65533: "FFFD",
    #               65534: "FFFE",
    #               65535: "FFFF", },
    #     #entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "Timing: Charge",
    #     key = "ro_timing_charge",
    #     register = 0x1112,
    #     scale = { 0: "Enabled - Charging & Discharging",
    #               1: "Enabled - Charging",
    #               2: "Enabled - Discharging",
    #               4: "Disabled", },
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "Timing: Charge Start Time",
    #     key = "ro_timing_charge_start_time",
    #     register = 0x1113,
    #     scale = value_function_sofartime,
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    #     icon = "mdi:battery-clock",
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "Timing: Charge End Time",
    #     key = "ro_timing_charge_end_time",
    #     register = 0x1114,
    #     scale = value_function_sofartime,
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    #     icon = "mdi:battery-clock",
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "Timing: Discharge Start Time",
    #     key = "ro_timing_discharge_start_time",
    #     register = 0x1115,
    #     scale = value_function_sofartime,
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    #     icon = "mdi:battery-clock",
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "Timing: Discharge End Time",
    #     key = "ro_timing_discharge_end_time",
    #     register = 0x1116,
    #     scale = value_function_sofartime,
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    #     icon = "mdi:battery-clock",
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "Timing: Charge Power",
    #     key = "timing_charge_power",
    #     native_unit_of_measurement = UnitOfPower.WATT,
    #     device_class = SensorDeviceClass.POWER,
    #     register = 0x1117,
    #     unit = REGISTER_U32,
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "Timing: Discharge Power",
    #     key = "timing_discharge_power",
    #     native_unit_of_measurement = UnitOfPower.WATT,
    #     device_class = SensorDeviceClass.POWER,
    #     register = 0x1119,
    #     unit = REGISTER_U32,
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "Timing: Control",
    #     key = "timeing_control",
    #     register = 0x111F,
    #     scale = { 0: "0",
    #               1: "1",
    #               2: "2",
    #               3: "3",
    #               65531: "FFFB",
    #               65532: "FFFC",
    #               65533: "FFFD",
    #               65534: "FFFE",
    #               65535: "FFFF", },
    #     #entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "TOU: ID",
    #     key = "tou_id",
    #     register = 0x1120,
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "TOU: Control",
    #     key = "ro_tou_control",
    #     register = 0x1121,
    #     scale = { 0: "Disabled",
    #               1: "Enabled", },
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "TOU: Charge Start Time",
    #     key = "ro_tou_charge_start_time",
    #     register = 0x1122,
    #     scale = value_function_sofartime,
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    #     icon = "mdi:battery-clock",
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "TOU: Charge End Time",
    #     key = "ro_tou_charge_end_time",
    #     register = 0x1123,
    #     scale = value_function_sofartime,
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    #     icon = "mdi:battery-clock",
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "TOU: Target SOC",
    #     key = "tou_target_soc",
    #     register = 0x1124,
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    # ),
    # SofarModbusSensorEntityDescription(
    #     name = "TOU Charge Power",
    #     key = "tou_charge_power",
    #     native_unit_of_measurement = UnitOfPower.WATT,
    #     device_class = SensorDeviceClass.POWER,
    #     register = 0x1125,
    #     unit = REGISTER_U32,
    #     entity_registry_enabled_default =  False,
    #     allowedtypes = HYBRID,
    # ),
    SofarModbusSensorEntityDescription(
        name="Update System Time Operation Result",
        key="sync_rtc_result",
        register=0x100A,
        scale={
            0: "Successful",
            1: "Operation in progress",
            2: "Enabled - Discharging",
            4: "Disabled",
            65531: "Operation failed, controller refused to respond",
            65532: "Operation failed, no response from the controller",
            65533: "Operation failed, current function disabled",
            65534: "Operation failed, parameter access failed",
            65535: "Operation failed, input parameters incorrect",
        },
        allowedtypes=HYBRID,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Passive: Timeout",
        key="passive_mode_timeout",
        register=0x1184,
        entity_registry_enabled_default=False,
        allowedtypes=HYBRID,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Passive: Timeout Action",
        key="passive_mode_timeout_action",
        register=0x1185,
        internal=True,
        scale={
            0: "Force Standby",
            1: "Return to Previous Mode",
        },
        allowedtypes=HYBRID,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Passive: Desired Grid Power",
        key="passive_mode_grid_power",
        register_data_type=REGISTER_S32,
        internal=True,
        register=0x1187,
        allowedtypes=HYBRID,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Passive: Minimum Battery Power",
        key="passive_mode_battery_power_min",
        register_data_type=REGISTER_S32,
        register=0x1189,
        internal=True,
        allowedtypes=HYBRID,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SofarModbusSensorEntityDescription(
        name="Passive: Maximum Battery Power",
        key="passive_mode_battery_power_max",
        register_data_type=REGISTER_S32,
        register=0x118B,
        internal=True,
        allowedtypes=HYBRID,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]


BATTERY_SENSOR_TYPES: list[SofarModbusSensorEntityDescription] = [
    SofarModbusSensorEntityDescription(
        name="BMS Version",
        key="bms_version",
        native_unit_of_measurement=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        register=0x900B,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Realtime Capacity",
        key="realtime_capacity",
        native_unit_of_measurement=PERCENTAGE,
        register=0x900E,
        allowedtypes=BAT_BTS,
        icon="mdi:battery-clock",
    ),
    # On BTS the whole 0x9000 area follows the pack selection, so these are per-module values;
    # HYD-EP keeps its original names.
    *[
        SofarModbusSensorEntityDescription(
            name=f"{prefix} Voltage",
            key="total_voltage",
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            register=0x900F,
            scale=0.1,
            **variant,
        )
        for prefix, variant in (("Module", {"allowedtypes": BAT_BTS, "blacklist": ["SM2ES4"]}), ("Total", {"allowedtypes": HYD_EP}))
    ],
    *[
        SofarModbusSensorEntityDescription(
            name=f"{prefix} Current",
            key="total_current",
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            device_class=SensorDeviceClass.CURRENT,
            register=0x9010,
            register_data_type=REGISTER_S16,
            scale=0.1,
            read_scale_exceptions=HYD_EP_CURRENT_SCALE_EXCEPTIONS,
            **variant,
        )
        for prefix, variant in (("Module", {"allowedtypes": BAT_BTS, "blacklist": ["SM2ES4"]}), ("Total", {"allowedtypes": HYD_EP}))
    ],
    SofarModbusSensorEntityDescription(
        name="SOC",
        key="soc",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        register=0x9012,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="SOH",
        key="soh",
        native_unit_of_measurement=PERCENTAGE,
        register=0x9013,
        allowedtypes=BAT_BTS,
        icon="mdi:battery-heart",
    ),
    SofarModbusSensorEntityDescription(
        name="Pack ID",
        key="pack_id",
        newblock=True,
        register=0x9044,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Pack Time",
        key="pack_time",
        register=0x9045,
        register_data_type=REGISTER_U32,
        scale=value_function_2byte_timestamp,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
        icon="mdi:clock",
    ),
    SofarModbusSensorEntityDescription(
        name="Pack Serial Number",
        key="pack_serial_number",
        register=0x9047,
        newblock=True,
        register_data_type=REGISTER_STR,
        wordcount=10,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="cell {} voltage",
        key="cell_{}_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x9051,
        suggested_display_precision=3,
        scale=0.001,
        rounding=3,
        allowedtypes=BAT_BTS,
        value_series=16,
    ),
    SofarModbusSensorEntityDescription(
        name="cell max voltage",
        key="cell_max_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x9069,
        suggested_display_precision=3,
        scale=0.001,
        rounding=3,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="cell min voltage",
        key="cell_min_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x906A,
        suggested_display_precision=3,
        scale=0.001,
        rounding=3,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Pack Temperature {}",
        key="pack_temperature_{}",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x906B,
        register_data_type=REGISTER_S16,
        scale=0.1,
        allowedtypes=BAT_BTS,
        value_series=4,
    ),
    SofarModbusSensorEntityDescription(
        name="Pack Temperature MOS",
        key="pack_temperature_mos",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x906F,
        register_data_type=REGISTER_S16,
        scale=0.1,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Pack Temperature Env",
        key="pack_temperature_env",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x9070,
        register_data_type=REGISTER_S16,
        scale=0.1,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Pack Current",
        key="pack_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x9071,
        register_data_type=REGISTER_S16,
        scale=0.1,
        read_scale_exceptions=HYD_EP_CURRENT_SCALE_EXCEPTIONS,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Pack remaining capacity",
        key="pack_remaining_capacity",
        native_unit_of_measurement="Ah",
        register=0x9072,
        scale=0.1,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Pack Full charge capacity",
        key="pack_full_charge_capacity",
        native_unit_of_measurement="Ah",
        register=0x9073,
        scale=0.1,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Pack Cycles",
        key="pack_cycles",
        register=0x9074,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Pack Total Voltage",
        key="pack_total_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x9079,
        scale=0.1,
        suggested_display_precision=1,
        allowedtypes=BAT_BTS | HYD_EP,
    ),
    SofarModbusSensorEntityDescription(
        name="Pack SOC",
        key="pack_soc",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        register=0x907A,
        scale=0.1,
        suggested_display_precision=1,
        allowedtypes=BAT_BTS | HYD_EP,
    ),
]


PCU_WARNING_TEXTS = {
    (0, 1): "Charge soft start failure",
    (0, 2): "Discharge soft start failure",
    (0, 3): "Fan 1 failure",
    (0, 4): "Fan 2 failure",
    (0, 5): "Fan 3 failure",
    (0, 6): "Fan 4 failure",
    (0, 7): "PCU version inconsistency",
    (0, 8): "Radiator 1 over-temperature",
    (0, 9): "Ambient over-temperature",
    (0, 10): "Radiator 2 over-temperature",
    (0, 13): "Charge prohibited",
    (0, 14): "Discharge prohibited",
    (0, 15): "Battery unbalanced",
    (1, 0): "Bus overvoltage",
    (1, 1): "Bus undervoltage",
    (1, 2): "Battery overvoltage",
    (1, 3): "Battery undervoltage",
}

PCU_FAULT_TEXTS = {
    (0, 0): "Radiator 1 over-temperature protection",
    (0, 1): "Ambient over-temperature protection",
    (0, 2): "SCI communication failure",
    (0, 3): "CAN1 communication failure",
    (0, 4): "Relay 1 failure",
    (0, 5): "Radiator 2 over-temperature protection",
    (0, 6): "CAN2 communication failure",
    (0, 7): "Relay 2 failure",
    (0, 8): "Bus software overvoltage",
    (0, 9): "Bus software undervoltage",
    (0, 10): "Battery software overvoltage",
    (0, 11): "Battery software undervoltage",
    (0, 12): "Battery software overcurrent",
    (0, 13): "Flying capacitor 1 overvoltage",
    (0, 14): "Flying capacitor 2 overvoltage",
    (0, 15): "Hardware overcurrent",
    (1, 0): "Permanent bus overvoltage",
    (1, 1): "Permanent battery undervoltage",
    (1, 2): "Permanent overcurrent",
    (1, 3): "Permanent hardware overcurrent",
    (1, 4): "Flying capacitor 3 overvoltage",
    (1, 5): "Flying capacitor 4 overvoltage",
    (1, 6): "Flying capacitor 1 undervoltage",
    (1, 7): "Flying capacitor 2 undervoltage",
    (1, 8): "Relay 1 permanent failure",
    (1, 9): "Relay 2 permanent failure",
    (1, 10): "Flying capacitor 3 undervoltage",
    (1, 11): "Flying capacitor 4 undervoltage",
    (1, 13): "Permanent bus short circuit",
    (1, 14): "Permanent battery activation failure",
    (1, 15): "Permanent bus reverse connection",
    (2, 0): "Battery status error",
    (2, 1): "PWM mode error",
    (2, 2): "BMS version error",
    (2, 3): "BMS overvoltage/overcurrent",
    (2, 4): "Battery average overcurrent",
    (2, 5): "Average overload",
    (2, 6): "Bus software overcurrent",
    (2, 7): "CBC overcurrent",
    (2, 8): "Pack ID error",
    (2, 9): "Start-up bus short circuit",
    (2, 10): "Bus average undervoltage",
    (2, 11): "Chip clock failure",
    (2, 12): "Inverter CAN communication failure",
    (2, 13): "Radiator low temperature",
    (2, 14): "Ambient low temperature",
    (2, 15): "Sampling offset calibration fault",
}

BDU_FAULT_TEXTS = {
    (0, 0): "Battery reverse connection",
    (0, 1): "Fuse fault",
}


def value_function_bit_texts(texts: dict[tuple[int, int], str]) -> Callable[[Any, Any, dict[str, Any]], str]:
    """Translate the set bits of one or more status words into a readable list."""

    def decode(initval: Any, descr: Any, datadict: dict[str, Any]) -> str:
        words = initval if isinstance(initval, list) else [initval]
        active = [
            texts.get((word_nr, bit), f"Unknown bit {word_nr + 1}.{bit}" if len(words) > 1 else f"Unknown bit {bit}")
            for word_nr, word in enumerate(words)
            for bit in range(16)
            if word >> bit & 1
        ]
        return ", ".join(active)[:255] if active else "None"

    return decode


def value_function_pcu_version(initval: Any, descr: Any, datadict: dict[str, Any]) -> str:
    """Decode "V" followed by three binary digits, e.g. 0x5601 0x0021 -> V01.00.33."""
    high, low = initval
    return f"{chr(high >> 8)}{high & 0xFF:02d}.{low >> 8:02d}.{low & 0xFF:02d}"


def value_function_bdu_version(initval: Any, descr: Any, datadict: dict[str, Any]) -> str:
    """Decode stage character and three ASCII digit pairs, e.g. V01.00.10."""
    stage, *parts = initval
    return chr(stage & 0xFF) + ".".join(part.to_bytes(2, "big").decode("ascii", errors="ignore") for part in parts)


def value_function_hardware_version(initval: Any, descr: Any, datadict: dict[str, Any]) -> str:
    return f"{initval >> 8}.{initval & 0xFF}"


BDU_SENSOR_TYPES: list[SofarModbusSensorEntityDescription] = [
    SofarModbusSensorEntityDescription(
        key="id",
        register=0x6090,
        newblock=True,
        internal=True,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Serial Number",
        key="serial_number",
        register=0x6091,
        register_data_type=REGISTER_STR,
        wordcount=10,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Software Version",
        key="software_version",
        register=0x609B,
        register_data_type=REGISTER_WORDS,
        wordcount=4,
        scale=value_function_bdu_version,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Fault",
        key="fault",
        register=0x609F,
        scale=value_function_bit_texts(BDU_FAULT_TEXTS),
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
        icon="mdi:alert-circle-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="Battery Modules",
        key="battery_modules",
        register=0x60A0,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
        icon="mdi:battery-unknown",
    ),
]

PCU_SENSOR_TYPES: list[SofarModbusSensorEntityDescription] = [
    # Inverter battery inputs, read in the same poll as each PCU for the DC/DC efficiency.
    *[
        SofarModbusSensorEntityDescription(
            key=f"battery_{quantity}_{nr}",
            register=address,
            register_data_type=REGISTER_S16 if quantity == "current" else REGISTER_U16,
            scale=0.01 if quantity == "current" else 0.1,
            newblock=address == 0x604,
            internal=True,
            scan_group=SCAN_GROUP_MEDIUM,
            allowedtypes=BAT_BTS,
        )
        for nr, base in ((1, 0x604), (2, 0x60B))
        for quantity, address in (("voltage", base), ("current", base + 1))
    ],
    SofarModbusSensorEntityDescription(
        key="id",
        register=0x6010,
        newblock=True,
        internal=True,
        scan_group=SCAN_GROUP_MEDIUM,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Serial Number",
        key="serial_number",
        register=0x6011,
        register_data_type=REGISTER_STR,
        wordcount=10,
        scan_group=SCAN_GROUP_MEDIUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Software Version",
        key="software_version",
        register=0x601B,
        register_data_type=REGISTER_WORDS,
        wordcount=2,
        scale=value_function_pcu_version,
        scan_group=SCAN_GROUP_MEDIUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Hardware Version",
        key="hardware_version",
        register=0x601D,
        scale=value_function_hardware_version,
        scan_group=SCAN_GROUP_MEDIUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="State",
        key="state",
        register=0x6020,
        scale={
            0: "Update",
            1: "Check",
            2: "Normal",
            3: "Active",
            4: "Fault",
            5: "Permanent Fault",
        },
        scan_group=SCAN_GROUP_MEDIUM,
        allowedtypes=BAT_BTS,
        icon="mdi:state-machine",
    ),
    SofarModbusSensorEntityDescription(
        name="Internal Temperature",
        key="internal_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        register=0x6021,
        scan_group=SCAN_GROUP_MEDIUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Radiator Temperature",
        key="radiator_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        register=0x6022,
        register_data_type=REGISTER_S16,
        scan_group=SCAN_GROUP_MEDIUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Warning",
        key="warning",
        register=0x6024,
        register_data_type=REGISTER_WORDS,
        wordcount=4,
        scale=value_function_bit_texts(PCU_WARNING_TEXTS),
        scan_group=SCAN_GROUP_MEDIUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
        icon="mdi:alert-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="Fault",
        key="fault",
        register=0x6028,
        register_data_type=REGISTER_WORDS,
        wordcount=4,
        scale=value_function_bit_texts(PCU_FAULT_TEXTS),
        scan_group=SCAN_GROUP_MEDIUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
        icon="mdi:alert-circle-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="High Side Voltage",
        key="high_side_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x602C,
        scale=0.1,
        rounding=1,
        scan_group=SCAN_GROUP_MEDIUM,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Low Side Voltage",
        key="low_side_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        register=0x602D,
        scale=0.1,
        rounding=1,
        scan_group=SCAN_GROUP_MEDIUM,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Low Side Current",
        key="low_side_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        register=0x602E,
        register_data_type=REGISTER_S16,
        scale=0.1,
        rounding=1,
        scan_group=SCAN_GROUP_MEDIUM,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Power",
        key="power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        register=0x602F,
        register_data_type=REGISTER_S16,
        scale=10,
        scan_group=SCAN_GROUP_MEDIUM,
        allowedtypes=BAT_BTS,
    ),
]


def value_function_bit_count(initval: Any, descr: Any, datadict: dict[str, Any]) -> int:
    return int(initval).bit_count()


def value_function_difference(initval: Any, descr: Any, datadict: dict[str, Any]) -> float:
    minuend, subtrahend = descr.depends_on
    return round(float(datadict[minuend]) - float(datadict[subtrahend]), 3)


def value_function_stored(initval: Any, descr: Any, datadict: dict[str, Any]) -> Any:
    return datadict.get(descr.key)


# BTS 5K BMS: SOC 0 % still leaves about 14 % of the full charge capacity (measured on four modules).
BMS_RESERVE_SHARE = 0.14
SOC_FIT_MIN_SPAN = 20  # % SOC needed before the learned Ah per % replaces the default


@dataclass
class soc_capacity_fit:
    """Least-squares fit of remaining capacity (Ah) over SOC (%), learning the Ah per SOC percent."""

    n: int = 0
    sum_soc: float = 0.0
    sum_capacity: float = 0.0
    sum_soc2: float = 0.0
    sum_soc_capacity: float = 0.0
    soc_min: float = 100.0
    soc_max: float = 0.0

    def add(self, soc: float, capacity: float) -> None:
        self.n += 1
        self.sum_soc += soc
        self.sum_capacity += capacity
        self.sum_soc2 += soc * soc
        self.sum_soc_capacity += soc * capacity
        self.soc_min = min(self.soc_min, soc)
        self.soc_max = max(self.soc_max, soc)

    @property
    def ah_per_percent(self) -> float | None:
        if self.soc_max - self.soc_min < SOC_FIT_MIN_SPAN:
            return None
        variance = self.n * self.sum_soc2 - self.sum_soc**2
        return (self.n * self.sum_soc_capacity - self.sum_soc * self.sum_capacity) / variance


# Bit meanings of the pack alarm/protection/fault words are not documented by SOFAR.
BATTERY_EXTENDED_SENSOR_TYPES: list[SofarModbusSensorEntityDescription] = [
    SofarModbusSensorEntityDescription(
        name="Cell Voltage Difference",
        key="cell_voltage_difference",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        suggested_display_precision=0,
        value_function=value_function_difference,
        depends_on=["cell_max_voltage", "cell_min_voltage"],
        allowedtypes=BAT_BTS,
        icon="mdi:sigma",
    ),
    # Energy the inverter can still draw down to its depth of discharge; set by check_battery_on_end.
    SofarModbusSensorEntityDescription(
        name="Remaining Energy",
        key="remaining_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_function=value_function_stored,
        depends_on=[],
        recompute_each_poll=True,
        allow_none=True,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Usable Energy",
        key="usable_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_function=value_function_stored,
        depends_on=[],
        recompute_each_poll=True,
        allow_none=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
    ),
    SofarModbusSensorEntityDescription(
        name="Balancing Cells",
        key="balancing_cells",
        register=0x9075,
        scale=value_function_bit_count,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
        icon="mdi:scale-balance",
    ),
    SofarModbusSensorEntityDescription(
        name="Alarm",
        key="pack_alarm",
        register=0x9076,
        scale=value_function_bit_texts({}),
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
        icon="mdi:alert-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="Protection",
        key="pack_protection",
        register=0x9077,
        scale=value_function_bit_texts({}),
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
        icon="mdi:shield-alert-outline",
    ),
    SofarModbusSensorEntityDescription(
        name="Fault",
        key="pack_fault",
        register=0x9078,
        scale=value_function_bit_texts({}),
        entity_category=EntityCategory.DIAGNOSTIC,
        allowedtypes=BAT_BTS,
        icon="mdi:alert-circle-outline",
    ),
]


DCDC_WINDOW_SECONDS = 600
DCDC_MIN_POWER = 50  # W; below this the direction of the power flow is not reliable
DCDC_MIN_SAMPLES = 10


DCDC_SENSOR_TYPES: list[SofarModbusSensorEntityDescription] = [
    SofarModbusSensorEntityDescription(
        name="DC/DC Loss",
        key="dcdc_loss",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_function=value_function_stored,
        depends_on=[],
        recompute_each_poll=True,
        allow_none=True,
        allowedtypes=BAT_BTS,
        icon="mdi:fire",
    ),
    *[
        SofarModbusSensorEntityDescription(
            name=f"DC/DC Efficiency {direction.capitalize()}",
            key=f"dcdc_efficiency_{direction}",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_function=value_function_stored,
            depends_on=[],
            recompute_each_poll=True,
            allow_none=True,
            allowedtypes=BAT_BTS,
            icon="mdi:percent-circle-outline",
        )
        for direction in ("charge", "discharge")
    ],
    SofarModbusSensorEntityDescription(
        name="Battery to AC Efficiency",
        key="battery_to_ac_efficiency",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_function=value_function_stored,
        depends_on=[],
        recompute_each_poll=True,
        allow_none=True,
        allowedtypes=BAT_BTS,
        icon="mdi:percent-circle-outline",
    ),
]

INVERTER_EFFICIENCY_SENSOR_TYPES: list[SofarModbusSensorEntityDescription] = [
    SofarModbusSensorEntityDescription(
        name="Conversion Loss",
        key="conversion_loss",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_function=value_function_stored,
        depends_on=[],
        recompute_each_poll=True,
        allow_none=True,
        allowedtypes=HYBRID | BAT_BTS,
        icon="mdi:fire",
    ),
    SofarModbusSensorEntityDescription(
        name="Conversion Efficiency",
        key="conversion_efficiency",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_function=value_function_stored,
        depends_on=[],
        recompute_each_poll=True,
        allow_none=True,
        allowedtypes=HYBRID | BAT_BTS,
        icon="mdi:percent-circle-outline",
    ),
]


@dataclass
class energy_ratio_window:
    """Sliding-window loss and efficiency as ratio of output to input energy."""

    seconds: float = DCDC_WINDOW_SECONDS
    samples: deque[tuple[float, float, float, bool]] = field(default_factory=deque)
    efficiency: float | None = None  # None while the window holds too few counting samples

    def add(self, now: float, power_in: float, power_out: float, counts: bool = True) -> None:
        """Add a sample; only samples with counts=True and enough input power enter the efficiency."""
        self.samples.append((now, power_in, power_out, counts))
        while self.samples[0][0] < now - self.seconds:
            self.samples.popleft()
        pairs = [(p_in, p_out) for _t, p_in, p_out, used in self.samples if used and p_in >= DCDC_MIN_POWER]
        if len(pairs) < DCDC_MIN_SAMPLES:
            self.efficiency = None
            return
        ratio = sum(p_out for _p_in, p_out in pairs) / sum(p_in for p_in, _p_out in pairs)
        if ratio <= 1:  # above 1 only measurement skew; keep the last value of this run
            self.efficiency = round(100 * ratio, 1)

    @property
    def loss(self) -> float | None:
        if len(self.samples) < DCDC_MIN_SAMPLES:
            return None
        return round(sum(p_in - p_out for _t, p_in, p_out, _used in self.samples) / len(self.samples))


@dataclass
class dcdc_efficiency_window:
    """Sliding-window DC/DC loss and efficiency from paired high-side and summed low-side power.

    Power is negative while discharging on both sides. Efficiencies are energy ratios over the
    window, so samples taken at slightly different times average out.
    """

    seconds: float = DCDC_WINDOW_SECONDS
    samples: deque[tuple[float, float, float]] = field(default_factory=deque)
    efficiency: dict[str, float] = field(default_factory=dict)  # only directions measured within the window

    def add(self, now: float, high_side: float, low_side: float) -> None:
        self.samples.append((now, high_side, low_side))
        while self.samples[0][0] < now - self.seconds:
            self.samples.popleft()
        for direction, sign in (("charge", 1), ("discharge", -1)):
            pairs = [(abs(high), abs(low)) for _t, high, low in self.samples if sign * low >= DCDC_MIN_POWER and sign * high > 0]
            if len(pairs) < DCDC_MIN_SAMPLES:
                self.efficiency.pop(direction, None)
                continue
            high_energy = sum(high for high, _low in pairs)
            low_energy = sum(low for _high, low in pairs)
            ratio = low_energy / high_energy if direction == "charge" else high_energy / low_energy
            if ratio <= 1:  # above 1 only measurement skew; keep the last value of this run
                self.efficiency[direction] = round(100 * ratio, 1)

    @property
    def loss(self) -> float | None:
        if len(self.samples) < DCDC_MIN_SAMPLES:
            return None
        return round(sum(high - low for _t, high, low in self.samples) / len(self.samples))


@dataclass(kw_only=True)
class sofar_selectable_level(battery_device_level):
    """BTS device level read through a volatile query register and its ID readback."""

    inquire_address: int
    id_address: int

    async def read_u16(self, hub: Any, address: int) -> int | None:
        try:
            response = await hub.async_read_holding_registers(unit=hub._modbus_addr, address=address, count=1)
        except Exception:
            _LOGGER.warning("Cannot read register 0x%x", address, exc_info=True)
            return None
        if response is None or response.isError():
            return None
        return int(response.registers[0])

    async def select_number(self, hub: Any, number: int) -> bool:
        if await self.read_u16(hub, self.id_address) == number:
            return True
        try:
            await hub.async_write_registers_single(unit=hub._modbus_addr, address=self.inquire_address, payload=number)
        except Exception:
            _LOGGER.warning("Cannot select %s %s via register 0x%x", self.name, number, self.inquire_address, exc_info=True)
            return False
        for _attempt in range(5):
            await asyncio.sleep(0.2)
            if await self.read_u16(hub, self.id_address) == number:
                return True
        _LOGGER.warning("Inverter did not select %s %s", self.name, number)
        return False

    async def answers(self, hub: Any, number: int) -> bool:
        """Return whether the device reports a serial number; the inverter accepts any query value."""
        if not await self.select_number(hub, number):
            return False
        response = await hub.async_read_holding_registers(unit=hub._modbus_addr, address=self.id_address + 1, count=10)
        return response is not None and not response.isError() and any(response.registers)

    async def select(self, hub: Any, instance: battery_device_instance) -> bool:
        return await self.select_number(hub, instance.number)

    async def verify(self, hub: Any, instance: battery_device_instance, new_data: dict[str, Any]) -> bool:
        if new_data.get(instance.key_prefix + "id") != instance.number or not new_data.get(instance.key_prefix + "serial_number"):
            _LOGGER.warning("Validation failed for %s: unexpected ID or missing serial number", instance.name)
            return False
        return True

    def get_device_details(self, new_data: dict[str, Any], key_prefix: str) -> dict[str, str]:
        details = {
            "serial_number": new_data.get(key_prefix + "serial_number"),
            "sw_version": new_data.get(key_prefix + "software_version"),
            "hw_version": new_data.get(key_prefix + "hardware_version"),
        }
        return {field: value for field, value in details.items() if value}


@dataclass(kw_only=True)
class sofar_pcu_level(sofar_selectable_level):
    """PCUs of one tower; also feeds the tower's DC/DC loss and efficiency."""

    tower_key_prefix: str
    window: dcdc_efficiency_window = field(default_factory=dcdc_efficiency_window)
    low_side: dict[int, tuple[float, float]] = field(default_factory=dict)  # PCU number -> (time, W)

    async def verify(self, hub: Any, instance: battery_device_instance, new_data: dict[str, Any]) -> bool:
        if not await super().verify(hub, instance, new_data):
            return False
        prefix = instance.key_prefix
        try:
            low = new_data[prefix + "low_side_voltage"] * new_data[prefix + "low_side_current"]
            high = sum(new_data[f"{prefix}battery_voltage_{nr}"] * new_data[f"{prefix}battery_current_{nr}"] for nr in (1, 2))
        except (KeyError, TypeError):
            return True
        now = wall_clock()
        self.low_side[instance.number] = (now, low)
        # Pair the fresh high-side value with the latest reading of every PCU.
        if len(self.low_side) == len(self.instances) and all(now - seen < 120 for seen, _low in self.low_side.values()):
            self.window.add(now, high, sum(value for _seen, value in self.low_side.values()))
            new_data[self.tower_key_prefix + "dcdc_loss"] = self.window.loss
            for direction in ("charge", "discharge"):
                new_data[f"{self.tower_key_prefix}dcdc_efficiency_{direction}"] = self.window.efficiency.get(direction)
        return True


@dataclass(kw_only=True)
class battery_config(base_battery_config):
    battery_sensor_type: list[SofarModbusSensorEntityDescription] | None = field(default_factory=lambda: BATTERY_SENSOR_TYPES)  # type: ignore[assignment]  # Covariant list typing
    battery_sensor_name_prefix: str | None = field(default="Battery {batt-nr}/{pack-nr} ")
    battery_sensor_key_prefix: str | None = field(default="battery_{batt-nr}_{pack-nr}_")

    bapack_number_address = 0x900D
    bms_inquire_address = 0x9020
    bms_check_address = 0x9044
    batt_pack_serial_address = 0x9047
    batt_pack_serial_len = 10
    batt_pack_model_address = 0x9007
    batt_pack_model_len = 4
    select_attempts = 3

    number_cels_in_parallel: int | None = None  # number of battery pack cells in parallel
    number_strings: int | None = None  # number of strings of all battery packs
    batt_pack_serials: dict[int, dict[int, str]] = field(default_factory=dict)
    selected_batt_nr: int | None = None
    selected_batt_pack_nr: int | None = None
    single_tower: bool = False  # set by async_get_extended_levels
    pcu_level: sofar_pcu_level | None = None  # set by async_get_extended_levels for a single tower
    inverter_window: energy_ratio_window = field(default_factory=energy_ratio_window)
    inverter_discharge_window: energy_ratio_window = field(default_factory=energy_ratio_window)
    inverter_sampled: float = float("-inf")
    depth_of_discharge: int | None = None  # 0x104D, refreshed by async_inverter_follow_up
    depth_of_discharge_read: float = float("-inf")
    capacity_fits: dict[str, soc_capacity_fit] = field(default_factory=dict)  # per pack key prefix

    def get_extended_inverter_sensor_types(self, hub: Any) -> list[Any]:
        return INVERTER_EFFICIENCY_SENSOR_TYPES if hub._invertertype & BAT_BTS else []

    async def _read_block(self, hub: Any, address: int, count: int) -> list[int] | None:
        try:
            response = await hub.async_read_holding_registers(unit=hub._modbus_addr, address=address, count=count)
        except Exception:
            _LOGGER.debug("Cannot read 0x%x for the efficiency", address, exc_info=True)
            return None
        if response is None or response.isError():
            return None
        return list(response.registers)

    async def async_inverter_follow_up(self, hub: Any, new_data: dict[str, Any]) -> None:
        """Sample PV, battery and AC power in one go for the inverter efficiency window."""
        now = wall_clock()
        if not hub._invertertype & BAT_BTS:
            return
        if now - self.depth_of_discharge_read >= 600 and (dod := await self._read_block(hub, 0x104D, 1)) is not None:
            self.depth_of_discharge_read = now
            self.depth_of_discharge = dod[0]
        if now - self.inverter_sampled < 10:
            return
        self.inverter_sampled = now
        ac = await self._read_block(hub, 0x485, 1)
        pv = await self._read_block(hub, 0x584, 6)  # voltage, current, power of MPPT 1 and 2
        battery = await self._read_block(hub, 0x604, 9)  # voltage and current of inputs 1 (0x604) and 2 (0x60B)
        if ac is None or pv is None or battery is None:
            return

        def s16(value: int) -> int:
            return value - 0x10000 if value & 0x8000 else value

        ac_power = s16(ac[0]) * 10  # positive: AC output, includes the EPS output
        pv_power = sum(pv[i] / 10 * pv[i + 1] / 100 for i in (0, 3))
        battery_power = sum(battery[i] / 10 * s16(battery[i + 1]) / 100 for i in (0, 7))  # negative: discharge
        power_in = pv_power + max(-battery_power, 0) + max(-ac_power, 0)
        power_out = max(ac_power, 0) + max(battery_power, 0)
        self.inverter_window.add(now, power_in, power_out)
        # Battery-only discharge isolates the battery to AC path.
        self.inverter_discharge_window.add(now, power_in, power_out, pv_power < 20 and battery_power <= -DCDC_MIN_POWER)
        new_data["conversion_loss"] = self.inverter_window.loss
        new_data["conversion_efficiency"] = self.inverter_window.efficiency
        dcdc = self.pcu_level.window.efficiency.get("discharge") if self.pcu_level else None
        inverter = self.inverter_discharge_window.efficiency
        if self.pcu_level is not None:
            new_data[self.pcu_level.tower_key_prefix + "battery_to_ac_efficiency"] = (
                round(dcdc * inverter / 100, 1) if dcdc is not None and inverter is not None else None
            )

    def get_extended_pack_sensor_types(self, hub: Any) -> list[Any]:
        return BATTERY_EXTENDED_SENSOR_TYPES if hub._invertertype & BAT_BTS else []

    async def async_get_extended_levels(self, hub: Any) -> list[battery_device_level]:
        self.single_tower = False
        self.pcu_level = None
        if not hub._invertertype & BAT_BTS:
            return []
        bdu = sofar_selectable_level(name="BDU", sensor_types=BDU_SENSOR_TYPES, inquire_address=0x60C4, id_address=0x6090)
        pcu = sofar_pcu_level(name="PCU", sensor_types=PCU_SENSOR_TYPES, inquire_address=0x6044, id_address=0x6010, tower_key_prefix="battery_1_bdu_")

        # BDU_Number reads 0 after an inverter restart although the BDU answers.
        towers = await bdu.read_u16(hub, 0x6084) or int(await bdu.answers(hub, 1))
        bdu.instances = [
            battery_device_instance(key=f"battery_{tower}", name=f"Battery {tower}", key_prefix=f"battery_{tower}_bdu_", number=tower)
            for tower in range(1, towers + 1)
        ]
        if towers > 1:
            # PCU numbers are not unique per tower and their tower assignment is unknown.
            _LOGGER.warning("%s: %s battery towers found; PCU devices are only supported with a single tower", hub.name, towers)
        elif towers == 1:
            self.single_tower = True
            self.pcu_level = pcu
            bdu.sensor_types = BDU_SENSOR_TYPES + DCDC_SENSOR_TYPES
            pcu.instances = [
                battery_device_instance(
                    key=f"battery_1_pcu_{number}",
                    name=f"Battery 1 PCU {number}",
                    key_prefix=f"battery_1_pcu_{number}_",
                    number=number,
                    parent_key="battery_1",
                )
                for number in range(1, (await pcu.read_u16(hub, 0x6004) or 0) + 1)
            ]
        return [bdu, pcu]

    def get_pack_parent_key(self, batt_nr: int) -> str | None:
        return "battery_1" if self.single_tower and batt_nr == 0 else None

    async def init_batt_pack(self, hub: Any, serial_number: str) -> None:
        assert self.selected_batt_nr is not None
        assert self.selected_batt_pack_nr is not None
        if not self.batt_pack_serials.__contains__(self.selected_batt_nr):
            self.batt_pack_serials[self.selected_batt_nr] = {}
        self.batt_pack_serials[self.selected_batt_nr][self.selected_batt_pack_nr] = serial_number

    async def get_batt_pack_quantity(self, hub: Any) -> int | None:
        if self.number_cels_in_parallel is None:
            await self._determine_bat_quantitys(hub)
        return self.number_cels_in_parallel

    async def get_batt_quantity(self, hub: Any) -> int | None:
        if self.number_strings is None:
            await self._determine_bat_quantitys(hub)
        return self.number_strings

    async def _read_selected_battery(self, hub: Any) -> int | None:
        """Return the battery selection reported by the inverter."""
        try:
            inverter_data = await hub.async_read_holding_registers(
                unit=hub._modbus_addr,
                address=self.bms_check_address,
                count=1,
            )
            if inverter_data is None:
                _LOGGER.warning("No response from BMS selection register 0x%x", self.bms_check_address)
                return None
            if inverter_data.isError():
                _LOGGER.warning("Modbus error reading BMS selection register 0x%x: %s", self.bms_check_address, inverter_data)
                return None

            return int(convert_from_registers(inverter_data.registers[:1], DataType.UINT16, "big"))  # type: ignore[attr-defined]  # DataType enum dynamic
        except Exception:
            _LOGGER.warning("Cannot read BMS selection register 0x%x", self.bms_check_address, exc_info=True)
            return None

    async def select_battery(self, hub: Any, batt_nr: int, batt_pack_nr: int) -> bool:
        faulty_nr = 0
        payload = faulty_nr << 12 | batt_pack_nr << 8 | batt_nr
        _LOGGER.debug("select batt-nr: %s batt-pack: %s 0x%x", batt_nr, batt_pack_nr, payload)

        selected_battery = await self._read_selected_battery(hub)
        if selected_battery == payload:
            self.selected_batt_nr = batt_nr
            self.selected_batt_pack_nr = batt_pack_nr
            return True

        # Another Modbus client may switch the selection concurrently, so write again before giving up.
        for _write in range(self.select_attempts):
            try:
                await hub.async_write_registers_single(unit=hub._modbus_addr, address=self.bms_inquire_address, payload=payload)
            except Exception:
                _LOGGER.warning(
                    "Cannot select batt_nr: %s, batt_pack_nr: %s via register 0x%x",
                    batt_nr,
                    batt_pack_nr,
                    self.bms_inquire_address,
                    exc_info=True,
                )
                return False
            for _check in range(3):
                await asyncio.sleep(0.3)
                if await self._read_selected_battery(hub) == payload:
                    self.selected_batt_nr = batt_nr
                    self.selected_batt_pack_nr = batt_pack_nr
                    return True

        _LOGGER.warning("Inverter did not select batt_nr: %s, batt_pack_nr: %s", batt_nr, batt_pack_nr)
        return False

    async def get_batt_pack_serial(self, hub: Any, batt_nr: int, batt_pack_nr: int) -> str | None:
        if not self.batt_pack_serials.__contains__(batt_nr):
            return None
        if not self.batt_pack_serials[batt_nr].__contains__(batt_pack_nr):
            return None
        return self.batt_pack_serials[batt_nr][batt_pack_nr]

    async def get_batt_pack_model(self, hub: Any) -> str | None:
        try:
            inverter_data = await hub.async_read_holding_registers(
                unit=hub._modbus_addr, address=self.batt_pack_model_address, count=self.batt_pack_model_len
            )
            if inverter_data is not None and not inverter_data.isError():
                raw = convert_from_registers(inverter_data.registers[: self.batt_pack_model_len], DataType.STRING, "big")  # type: ignore[attr-defined]  # DataType enum dynamic
                serial = raw.decode("ascii", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
                return serial
            return None
        except Exception:
            _LOGGER.warning("Cannot read batt pack model")
            return None

    async def get_batt_pack_sw_version(self, hub: Any, new_data: dict[str, Any], key_prefix: str) -> str | None:
        sw_version_key = key_prefix + "bms_version"
        if not new_data.__contains__(sw_version_key):
            _LOGGER.info("batt pack software version not received %s", sw_version_key)
            return None
        return f"BMS: V{new_data[sw_version_key]}"

    async def check_battery_on_start(self, hub: Any, old_data: dict[str, Any], key_prefix: str, batt_nr: int, batt_pack_nr: int) -> bool:
        faulty_nr = 0
        payload = faulty_nr << 12 | batt_pack_nr << 8 | batt_nr
        for _retry in range(0, 10):
            selected_battery = await self._read_selected_battery(hub)
            if selected_battery == payload:
                return True
            if selected_battery is not None:
                await asyncio.sleep(0.3)
        _LOGGER.warning(
            "BMS validation before reading failed for battery %s pack %s: register 0x%x expected 0x%04x, last selection %s",
            batt_nr,
            batt_pack_nr,
            self.bms_check_address,
            payload,
            f"0x{selected_battery:04x}" if selected_battery is not None else "unavailable",
        )
        return False

    async def check_battery_on_end(
        self, hub: Any, old_data: dict[str, Any], new_data: dict[str, Any], key_prefix: str, batt_nr: int, batt_pack_nr: int
    ) -> bool:
        """Reject unverified pack data without turning a validation failure into a poll exception."""
        faulty_nr = 0
        compare_value = faulty_nr << 12 | batt_pack_nr << 8 | batt_nr
        selected_battery = await self._read_selected_battery(hub)
        if selected_battery != compare_value:
            _LOGGER.warning(
                "BMS validation after reading failed for battery %s pack %s: register 0x%x expected 0x%04x, actual selection %s",
                batt_nr,
                batt_pack_nr,
                self.bms_check_address,
                compare_value,
                f"0x{selected_battery:04x}" if selected_battery is not None else "unavailable",
            )
            return False

        serial_key = key_prefix + "pack_serial_number"
        serial = new_data.get(serial_key)
        if not serial:
            _LOGGER.warning(
                "BMS validation after reading failed for battery %s pack %s: missing serial number (%s)", batt_nr, batt_pack_nr, serial_key
            )
            return False
        # Selection identifies the requested pack. A stored serial is device metadata,
        # not a permanent validation constraint (packs can be replaced or reordered).
        self.batt_pack_serials.setdefault(batt_nr, {})[batt_pack_nr] = serial
        self._update_pack_energy(new_data, key_prefix)
        return True

    def _update_pack_energy(self, new_data: dict[str, Any], key_prefix: str) -> None:
        """Energy above the inverter's discharge limit; SOC already excludes the BMS reserve."""
        try:
            soc = float(new_data[key_prefix + "soc"])
            remaining = float(new_data[key_prefix + "pack_remaining_capacity"])
            full = float(new_data[key_prefix + "pack_full_charge_capacity"])
            voltage = float(new_data[key_prefix + "total_voltage"])
        except (KeyError, TypeError, ValueError):
            return
        fit = self.capacity_fits.setdefault(key_prefix, soc_capacity_fit())
        fit.add(soc, remaining)
        ah_per_percent = fit.ah_per_percent or full * (1 - BMS_RESERVE_SHARE) / 100
        if self.depth_of_discharge is None:
            new_data[key_prefix + "remaining_energy"] = new_data[key_prefix + "usable_energy"] = None
            return
        floor = 100 - self.depth_of_discharge
        new_data[key_prefix + "remaining_energy"] = round(max(soc - floor, 0) * ah_per_percent * voltage / 1000, 2)
        new_data[key_prefix + "usable_energy"] = round(self.depth_of_discharge * ah_per_percent * voltage / 1000, 2)

    async def _determine_bat_quantitys(self, hub: Any) -> None:
        try:
            inverter_data = await hub.async_read_holding_registers(unit=hub._modbus_addr, address=self.bapack_number_address, count=1)
            if inverter_data is not None and not inverter_data.isError():
                val = convert_from_registers(inverter_data.registers[:1], DataType.UINT16, "big")  # type: ignore[attr-defined]  # DataType enum dynamic
                self.number_cels_in_parallel, self.number_strings = self._decode_battery_query_dimensions(hub.seriesnumber, int(val))
        except Exception:
            _LOGGER.warning("%s: attempt to read BaPack number failed at 0x%x", hub.name, self.bapack_number_address, exc_info=True)

    @staticmethod
    def _decode_battery_query_dimensions(seriesnumber: str, value: int) -> tuple[int, int]:
        """Return the pack and battery counts used for BMS_Inquire queries."""
        parallel_pack_count = (value >> 8) & 0xFF
        string_count = value & 0xFF

        if seriesnumber.startswith("SM2ES4"):
            # HYD-EP reports 0x0410 for four parallel packs with 16 cells
            # each. Its low byte is not another selectable BMS dimension.
            return 1, parallel_pack_count

        return parallel_pack_count, string_count

    async def init_batt_pack_serials(self, hub: Any) -> None:
        retry = 0
        assert self.number_strings is not None
        assert self.number_cels_in_parallel is not None
        while retry < 5:
            retry = retry + 1
            for batt_nr in range(self.number_strings):
                if not self.batt_pack_serials.__contains__(batt_nr):
                    self.batt_pack_serials[batt_nr] = {}

                for batt_pack_nr in range(self.number_cels_in_parallel):
                    if not await self.select_battery(hub, batt_nr, batt_pack_nr):
                        continue
                    serial = await self._determinate_batt_pack_serial(hub)
                    if self.batt_pack_serials[batt_nr].__contains__(batt_pack_nr):
                        if self.batt_pack_serials[batt_nr][batt_pack_nr] != serial:
                            retry = retry - 1
                    # type narrowing: serial is str | None, dict expects str
                    self.batt_pack_serials[batt_nr][batt_pack_nr] = serial  # type: ignore[assignment]  # serial can be None

        _LOGGER.info("serials %s", self.batt_pack_serials)

    async def _determinate_batt_pack_serial(self, hub: Any) -> str | None:
        inverter_data = await hub.async_read_holding_registers(
            unit=hub._modbus_addr, address=self.batt_pack_serial_address, count=self.batt_pack_serial_len
        )
        if inverter_data is not None and not inverter_data.isError():
            raw = convert_from_registers(inverter_data.registers[: self.batt_pack_serial_len], DataType.STRING, "big")  # type: ignore[attr-defined]  # DataType enum dynamic
            serial = raw.decode("ascii", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
            return serial
        return None


# ============================ plugin declaration =================================================


@dataclass(kw_only=True)
class sofar_plugin(plugin_base):
    """
    def isAwake(self, datadict):
        return (datadict.get('run_mode', None) == 'Normal Mode')

    def wakeupButton(self):
        return 'battery_awaken'
    """

    async def async_determineInverterType(self, hub: Any, configdict: dict[str, Any]) -> int:
        _LOGGER.info("%s: trying to determine inverter type", hub.name)
        seriesnumber = await async_read_serialnr(hub, 0x445, swapbytes=False)
        if not seriesnumber:
            _LOGGER.error("%s: cannot find serial number, even not for other Inverter", hub.name)
            seriesnumber = "unknown"

        # derive invertertype from seriiesnumber
        if seriesnumber.startswith("SP1ES120N6"):
            invertertype = HYBRID | X3  # HYD20KTL-3P no PV
            self.inverter_model = "HYD20KTL-3P"
        elif seriesnumber.startswith("SP1"):
            invertertype = HYBRID | X3 | GEN | BAT_BTS  # HYDxxKTL-3P
            self.inverter_model = "HYDxxKTL-3P"
        elif seriesnumber.startswith("SP2"):
            invertertype = HYBRID | X3 | GEN | BAT_BTS  # HYDxxKTL-3P 2nd type
            self.inverter_model = f"HYD{seriesnumber[6:8]}KTL-3P 2nd"
        elif seriesnumber.startswith("ZP1"):
            invertertype = HYBRID | X3 | GEN  # Azzurro HYDxx ZSS
            self.inverter_model = "HYDxx ZSS"
        elif seriesnumber.startswith("ZP2"):
            invertertype = HYBRID | X3 | GEN  # Azzurro HYDxx ZSS
            self.inverter_model = "HYDxx ZSS"
        elif seriesnumber.startswith("SM2E"):
            invertertype = HYBRID | X1 | GEN  # HYDxxxxES, Not actually X3, needs changing
            self.inverter_model = "HYDxxxxES"
            if seriesnumber.startswith("SM2ES4"):
                invertertype |= HYD_EP
                self.inverter_model = "HYD 3-6K-EP"
        elif seriesnumber.startswith("ZM2E"):
            invertertype = HYBRID | X1 | GEN  # HYDxxxxKTL ZCS HP, Single Phase
            self.inverter_model = "HYDxxxxKTL ZCS HP"
        elif seriesnumber.startswith("SH3E"):
            invertertype = PV | X1 | GEN  # 4.6 KTLM-G3
            self.inverter_model = "4.6 KTLM-G3"
        elif seriesnumber.startswith("SS2E"):
            invertertype = PV | X3 | GEN  # 4.4 KTLX-G3
            self.inverter_model = "4.4 KTLX-G3"
        elif seriesnumber.startswith("ZS2E"):
            invertertype = PV | X3 | GEN  # 12 Azzurro KTL-V3
            self.inverter_model = "12 Azzurro KTL-V3"
        elif seriesnumber.startswith("SQ1ES1"):
            invertertype = PV | X3 | GEN | MPPT10  # 100kW KTLX-G4
            self.inverter_model = "100kW KTLX-G4"
        elif seriesnumber.startswith("SA1"):
            invertertype = PV | X1  # Older Might be single
        elif seriesnumber.startswith("SB1"):
            invertertype = PV | X1  # Older Might be single
        elif seriesnumber.startswith("SC1"):
            invertertype = PV | X3  # Older Probably 3phase
        elif seriesnumber.startswith("SD1"):
            invertertype = PV | X3  # Older Probably 3phase
        elif seriesnumber.startswith("SF4"):
            invertertype = PV | X3  # Older Probably 3phase
        elif seriesnumber.startswith("SH1"):
            invertertype = HYBRID | X3 | GEN | BAT_BTS  # HYD5...8KTL-3P
            self.inverter_model = "HYD5...8KTL-3P"
        elif seriesnumber.startswith("SL1"):
            invertertype = PV | X3  # Older Probably 3phase
        elif seriesnumber.startswith("SJ2"):
            invertertype = PV | X3  # Older Probably 3phase
        elif seriesnumber.startswith("SS1"):
            invertertype = PV | X3 | GEN
        # elif seriesnumber.startswith('SM1E'):  plugin_sofar_old
        # elif seriesnumber.startswith('ZM1E'):  plugin_sofar_old

        else:
            invertertype = 0
            _LOGGER.error("unrecognized %s inverter type - serial number : %s", hub.name, seriesnumber)

        if invertertype > 0:
            read_eps = configdict.get(CONF_READ_EPS, DEFAULT_READ_EPS)
            read_dcb = configdict.get(CONF_READ_DCB, DEFAULT_READ_DCB)
            read_pm = configdict.get(CONF_READ_PM, DEFAULT_READ_PM)
            if read_eps:
                invertertype = invertertype | EPS
            if read_dcb:
                invertertype = invertertype | DCB
            if read_pm:
                invertertype = invertertype | PM

        return invertertype

    def matchInverterWithMask(self, inverterspec: int, entitymask: int, serialnumber: str = "not relevant", blacklist: Any = None) -> bool:
        # returns true if the entity needs to be created for an inverter
        genmatch = ((inverterspec & entitymask & ALL_GEN_GROUP) != 0) or (entitymask & ALL_GEN_GROUP == 0)
        xmatch = ((inverterspec & entitymask & ALL_X_GROUP) != 0) or (entitymask & ALL_X_GROUP == 0)
        hybmatch = ((inverterspec & entitymask & ALL_TYPE_GROUP) != 0) or (entitymask & ALL_TYPE_GROUP == 0)
        epsmatch = ((inverterspec & entitymask & ALL_EPS_GROUP) != 0) or (entitymask & ALL_EPS_GROUP == 0)
        dcbmatch = ((inverterspec & entitymask & ALL_DCB_GROUP) != 0) or (entitymask & ALL_DCB_GROUP == 0)
        pmmatch = ((inverterspec & entitymask & ALL_PM_GROUP) != 0) or (entitymask & ALL_PM_GROUP == 0)
        mpptmatch = ((inverterspec & entitymask & ALL_MPPT_GROUP) != 0) or (entitymask & ALL_MPPT_GROUP == 0)
        modelmatch = ((inverterspec & entitymask & ALL_MODEL_GROUP) != 0) or (entitymask & ALL_MODEL_GROUP == 0)
        blacklisted = False
        if blacklist:
            for start in blacklist:
                if serialnumber.startswith(start):
                    blacklisted = True
        return (genmatch and xmatch and hybmatch and epsmatch and dcbmatch and pmmatch and mpptmatch and modelmatch) and not blacklisted

    def getSoftwareVersion(self, new_data: dict[str, Any]) -> str | None:
        return new_data.get("software_version", None)

    def getHardwareVersion(self, new_data: dict[str, Any]) -> str | None:
        return new_data.get("hardware_version", None)

    def localDataCallback(self, hub: Any) -> bool:
        parallel_setting = hub.data.get("parallel_masterslave", "Slave")
        if parallel_setting == "Master":
            system_limit_w = hub.inverterPowerKw * 1000
            number_entity = hub.numberEntities.get("feedin_max_power")
            if number_entity:
                number_entity._attr_native_min_value = 0
                number_entity._attr_native_max_value = system_limit_w
                number_entity.entity_description = replace(
                    number_entity.entity_description,
                    native_min_value=0,
                    native_max_value=system_limit_w,
                )
                _LOGGER.info("Parallel Master: Set feedin_max_power limit to 0-%sW (inverter_power_kw=%skW)", system_limit_w, hub.inverterPowerKw)
        return True


plugin_instance = sofar_plugin(
    plugin_name="Sofar",
    plugin_manufacturer="Sofar Solar",
    SENSOR_TYPES=SENSOR_TYPES,
    NUMBER_TYPES=NUMBER_TYPES,
    BUTTON_TYPES=BUTTON_TYPES,
    SELECT_TYPES=SELECT_TYPES,
    SWITCH_TYPES=[],
    TIME_TYPES=[],
    BATTERY_CONFIG=battery_config(),
    block_size=48,
    order32="big",
    auto_block_ignore_readerror=True,
    default_holding_scangroup=SCAN_GROUP_DEFAULT,
    default_input_scangroup=SCAN_GROUP_DEFAULT,  # or SCAN_GROUP_AUTO
    auto_default_scangroup=SCAN_GROUP_FAST,  # only used when default_xxx_scangroup is set to SCAN_GROUP_AUTO
    auto_slow_scangroup=SCAN_GROUP_MEDIUM,  # only usedwhen default_xxx_scangroup is set to SCAN_GROUP_AUTO
)
