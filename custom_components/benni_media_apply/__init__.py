"""benni_media_apply — Ausführungsschicht / Executor (eigene HACS-Integration).

Single-Instance: ein Config-Entry (Profil benni/eltern) führt die media_policy-
Targets/Action idempotent + geramped an den echten Geräten aus. Konsumiert
benni_media_state + benni_media_policy über Entity-State (kein Import). Apply ist
gated (apply_enabled, Shadow-safe default OFF). Phase-1-Scaffold (kein Panel).
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DATA_COORDINATOR, DOMAIN, LEGACY_ENTITY_MAP
from .coordinator import MediaApplyCoordinator
from .services import async_remove_services, async_setup_services
from .view import async_remove_view
from .websocket_api import async_setup_websocket_api

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH]


def _migrate_value(value):
    if isinstance(value, str):
        return LEGACY_ENTITY_MAP.get(value, value)
    if isinstance(value, list):
        return [_migrate_value(item) for item in value]
    return value


def _migrated_entry_sources(entry: ConfigEntry) -> tuple[bool, dict, dict]:
    changed = False
    data = dict(entry.data)
    options = dict(entry.options)
    for target in (data, options):
        for key, value in list(target.items()):
            migrated = _migrate_value(value)
            if migrated != value:
                target[key] = migrated
                changed = True
    return changed, data, options


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    changed, data, options = _migrated_entry_sources(entry)
    if changed:
        hass.config_entries.async_update_entry(entry, data=data, options=options)
        _LOGGER.info("Migrated benni_media_apply entity bindings during setup")

    coord = MediaApplyCoordinator(hass, entry)
    await coord.async_load_stored()
    await coord.async_config_entry_first_refresh()
    coord.async_start()

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[entry.entry_id] = {DATA_COORDINATOR: coord}
    async_setup_services(hass)

    # WS-Contract einmalig registrieren (Single-Instance, aber gegen Mehrfach-Setup
    # geschützt). Der Flag-Key ist kein dict → vom _coordinator()-Scan ignoriert.
    if not domain_data.get("_ws_registered"):
        async_setup_websocket_api(hass)
        domain_data["_ws_registered"] = True
    async_remove_view(hass)  # FLEET-66: kein eigenes Panel mehr — benni_media-Umbrella ist die einzige Media-UI

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    entry.async_on_unload(coord.async_shutdown_ramp)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate retired entity IDs from earlier prefills."""
    changed, data, options = _migrated_entry_sources(entry)
    if changed or entry.version < 2:
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            options=options,
            version=2,
        )
        _LOGGER.info("Migrated benni_media_apply entity bindings")
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # Letzte Instanz weg → Panel entfernen.
        remaining = [
            v for v in hass.data.get(DOMAIN, {}).values()
            if isinstance(v, dict) and DATA_COORDINATOR in v
        ]
        if not remaining:
            async_remove_services(hass)
            async_remove_view(hass)
    return unloaded
