"""Native radio actions replace the three YAML scripts without a second owner."""
import asyncio
import importlib.util
import sys
import types
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import bma_const as C
from tests.test_resume_reconciliation import runtime as _runtime

runtime = _runtime


def test_start_service_uses_existing_single_flight(runtime):
    async def run():
        await runtime.coord.async_start_radio()
        owner = runtime.coord._playback_recovery_task
        await runtime.coord.async_start_radio()
        assert runtime.coord._playback_recovery_task is owner
        await owner
    asyncio.run(run())
    assert sum(action == "play_media" for _, action, _ in runtime.calls) == 1
    assert runtime.coord._playback_health == "healthy"


@pytest.mark.parametrize("change", [{"stop_latch": True}, {"bio_state": "sleep", "bio_sleep": True}, {"audio_owner": "private_stack"}, {"manual_playback": True}])
def test_start_service_respects_gates(runtime, change):
    runtime.inputs = replace(runtime.inputs, **change)
    asyncio.run(runtime.coord.async_start_radio())
    assert runtime.coord._playback_recovery_task is None
    assert runtime.calls == []


def test_start_respects_shadow_and_missing_binding_is_visible(runtime):
    runtime.inputs = replace(runtime.inputs, apply_enabled=False)
    runtime.coord.entry.options[C.CONF_APPLY_ENABLED] = False
    asyncio.run(runtime.coord.async_start_radio())
    assert runtime.calls == []
    del runtime.states["media_player.group"]
    with pytest.raises(ValueError, match="not bound"):
        asyncio.run(runtime.coord.async_start_radio())


def test_stop_cancels_pending_start_before_actuation_and_sets_latch_first(runtime):
    original = runtime.coord.hass.services.async_call
    async def service(domain, action, data, **kwargs):
        if action == "turn_on":
            runtime.inputs = replace(runtime.inputs, stop_latch=True)
            runtime.states[runtime.latch_entity].state = "on"
        await original(domain, action, data, **kwargs)
    runtime.coord.hass.services.async_call = service
    async def run():
        await runtime.coord.async_start_radio()
        owner = runtime.coord._playback_recovery_task
        await runtime.coord.async_stop_radio()
        await asyncio.gather(owner, return_exceptions=True)
    asyncio.run(run())
    assert [(domain, action) for domain, action, _ in runtime.calls] == [("homeassistant", "turn_on"), ("media_player", "media_stop")]
    assert runtime.inputs.stop_latch
    assert not runtime.coord._wake_start_owned


def test_clear_latch_has_no_play_side_effect_in_shadow(runtime):
    runtime.coord.entry.options[C.CONF_APPLY_ENABLED] = False
    runtime.inputs = replace(runtime.inputs, stop_latch=True)
    asyncio.run(runtime.coord.async_clear_stop_latch())
    assert runtime.calls == [("homeassistant", "turn_off", {"entity_id": runtime.latch_entity})]
    assert runtime.coord._playback_recovery_task is None


def test_missing_latch_does_not_silently_stop_without_restart_protection(runtime):
    del runtime.states[runtime.latch_entity]
    with pytest.raises(ValueError, match="binding unavailable"):
        asyncio.run(runtime.coord.async_stop_radio())
    assert runtime.calls == []


def test_stop_still_sets_latch_when_player_is_unavailable(runtime):
    runtime.states["media_player.group"].state = "unavailable"
    asyncio.run(runtime.coord.async_stop_radio())
    assert runtime.calls[0] == ("homeassistant", "turn_on", {"entity_id": runtime.latch_entity})
    assert runtime.calls[1][1] == "media_stop"


def test_native_service_handlers_follow_reload_and_are_removed(runtime, monkeypatch):
    registry = {}
    runtime.coord.hass.services.async_register = lambda domain, name, handler, schema: registry.__setitem__(name, handler)
    runtime.coord.hass.services.async_remove = lambda domain, name: registry.pop(name)
    runtime.coord.hass.data = {C.DOMAIN: {"entry": {C.DATA_COORDINATOR: runtime.coord}}}
    monkeypatch.setitem(sys.modules, "voluptuous", types.SimpleNamespace(Schema=lambda schema: schema))
    monkeypatch.setattr(sys.modules["homeassistant.core"], "ServiceCall", object, raising=False)
    class ValidationError(Exception):
        pass
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", types.SimpleNamespace(ServiceValidationError=ValidationError))
    spec = importlib.util.spec_from_file_location("bma_pure_pkg.services", Path(__file__).parents[1] / "custom_components/benni_media_apply/services.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.async_setup_services(runtime.coord.hass)
    assert set(registry) == {"start_radio", "stop_radio", "clear_stop_latch"}
    new_coord = types.SimpleNamespace(async_start_radio=AsyncMock())
    runtime.coord.hass.data[C.DOMAIN]["entry"][C.DATA_COORDINATOR] = new_coord
    asyncio.run(registry["start_radio"](types.SimpleNamespace(service="start_radio")))
    new_coord.async_start_radio.assert_awaited_once()
    runtime.coord.hass.data[C.DOMAIN].pop("entry")
    with pytest.raises(ValidationError, match="not loaded"):
        asyncio.run(registry["start_radio"](types.SimpleNamespace(service="start_radio")))
    module.async_remove_services(runtime.coord.hass)
    assert registry == {}
