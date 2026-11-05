"""Devices can be deleted from the UI once the integration no longer provides them."""

from collections.abc import AsyncGenerator
from typing import cast

import pytest
import pytest_asyncio
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_test_home_assistant

from custom_components.solax_modbus import async_remove_config_entry_device
from custom_components.solax_modbus.const import DOMAIN

ENTITY_ID = "sensor.sofar_battery_1_pcu_1_power"


@pytest_asyncio.fixture
async def hass() -> AsyncGenerator[HomeAssistant]:
    async with async_test_home_assistant() as hass:
        try:
            yield hass
        finally:
            await hass.async_stop(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state_attributes", "expected"),
    [
        pytest.param({}, False, id="provided"),
        pytest.param({"restored": True}, True, id="orphaned"),
        pytest.param(None, True, id="disabled-or-unloaded"),
    ],
)
async def test_only_devices_without_provided_entities_are_removable(
    hass: HomeAssistant, state_attributes: dict[str, bool] | None, expected: bool
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers=cast(set[tuple[str, str]], {(DOMAIN, "Sofar", "battery_1_pcu_1")})
    )
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "battery_1_pcu_1_power", config_entry=entry, device_id=device.id, suggested_object_id="sofar_battery_1_pcu_1_power"
    )
    if state_attributes is not None:
        hass.states.async_set(ENTITY_ID, "0", state_attributes)

    assert await async_remove_config_entry_device(hass, entry, device) is expected
