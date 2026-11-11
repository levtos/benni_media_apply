"""Native radio actions replacing the three legacy YAML scripts (#62)."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError

from .const import DATA_COORDINATOR, DOMAIN

RADIO_SERVICES = {
    "start_radio": "async_start_radio",
    "stop_radio": "async_stop_radio",
    "clear_stop_latch": "async_clear_stop_latch",
}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Resolve the loaded coordinator at call time, including after reload."""
    async def handle(call: ServiceCall) -> None:
        coordinator = next((bucket[DATA_COORDINATOR] for bucket in hass.data.get(DOMAIN, {}).values()
                            if isinstance(bucket, dict) and DATA_COORDINATOR in bucket), None)
        if coordinator is None:
            raise ServiceValidationError("Media Apply is not loaded")
        try:
            await getattr(coordinator, RADIO_SERVICES[call.service])()
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    for service in RADIO_SERVICES:
        hass.services.async_register(DOMAIN, service, handle, schema=vol.Schema({}))


@callback
def async_remove_services(hass: HomeAssistant) -> None:
    for service in RADIO_SERVICES:
        hass.services.async_remove(DOMAIN, service)
