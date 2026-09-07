"""Exercise the real #41 runner, shared admission and service payloads."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import bma_const as C
import bma_logic as L


@pytest.fixture
def runtime(monkeypatch):
    modules = {}
    for name in ("homeassistant", "homeassistant.config_entries", "homeassistant.core", "homeassistant.helpers", "homeassistant.helpers.event", "homeassistant.helpers.storage", "homeassistant.helpers.update_coordinator", "homeassistant.util", "homeassistant.util.dt"):
        modules[name] = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, modules[name])
    modules["homeassistant.config_entries"].ConfigEntry = object
    core = modules["homeassistant.core"]
    core.CALLBACK_TYPE = core.Event = core.HomeAssistant = object
    core.callback = lambda fn: fn
    events = modules["homeassistant.helpers.event"]
    events.async_call_later = events.async_track_state_change_event = lambda *args: lambda: None
    events.async_track_time_interval = lambda *args: lambda: None
    dt = modules["homeassistant.util.dt"]
    dt.utcnow = lambda: datetime.now(timezone.utc)
    modules["homeassistant.util"].dt = dt

    class Store:
        def __init__(self, *args):
            pass

    class Coordinator:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, hass, *args, **kwargs):
            self.hass, self.data = hass, {}

        def async_set_updated_data(self, data):
            self.data = data

        def async_update_listeners(self):
            pass

    modules["homeassistant.helpers.storage"].Store = Store
    modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = Coordinator
    name = "bma_pure_pkg.coordinator"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "custom_components/benni_media_apply/coordinator.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    env = types.SimpleNamespace(now=1.0, calls=[], states={}, replace_plays=True, on_wait=None, provider_error=False)
    env.inputs = L.Inputs(apply_enabled=True, volume_apply_allowed=True, action=C.ACTION_RESUME, homepods_should_pause=False, homepods_resume_allowed=True, homepods_target=0.3, quiet_mode=False, presence_state="anwesend", away_gate=False, stop_latch=False, radio_ready=True, manual_playback=False, planned_station_playing=False, bio_sleep=False, bio_state="awake", tv_power_on=False, audio_owner="homepods", radio_station="gayfm")
    group = "media_player.group"
    pods = ["media_player.pod1", "media_player.pod2"]
    for entity in [group, *pods]:
        env.states[entity] = types.SimpleNamespace(state="idle", attributes={"is_volume_muted": False})

    async def service(domain, action, data, **kwargs):
        env.calls.append((domain, action, data))
        if domain == "music_assistant":
            if env.provider_error:
                raise ValueError("private-url?token=must-not-appear")
            if env.replace_plays:
                for state in env.states.values():
                    state.state = "playing"
                env.inputs = replace(env.inputs, homepods_resume_allowed=False, action=C.ACTION_NONE, planned_station_playing=True)
        if action == "volume_mute":
            for entity in data["entity_id"]:
                env.states[entity].attributes["is_volume_muted"] = False

    hass = types.SimpleNamespace(states=types.SimpleNamespace(get=env.states.get), services=types.SimpleNamespace(async_call=service), loop=types.SimpleNamespace(time=lambda: env.now), async_create_task=asyncio.create_task)
    options = {C.CONF_APPLY_ENABLED: True, C.CONF_HOMEPODS_PLAYER: group, C.CONF_HOMEPODS_PODS: pods}
    entry = types.SimpleNamespace(data={}, options=options, entry_id="test")
    coord = module.MediaApplyCoordinator(hass, entry)
    monkeypatch.setattr(coord, "_build_inputs", lambda: env.inputs)
    monkeypatch.setattr(coord, "_float", lambda key: env.inputs.homepods_target)
    original_sleep = asyncio.sleep

    async def sleep(delay):
        env.now += delay
        if env.on_wait:
            env.on_wait()
        await original_sleep(0)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    env.coord = coord
    return env


def run_resume(env):
    async def run():
        plan = L.ApplyPlan(homepods_action=C.ACTION_RESUME, execute=True)
        env.coord._maybe_log(plan)
        await env.coord._execute(plan)
        task = env.coord._playback_recovery_task
        if task:
            await task
    asyncio.run(run())


def test_idle_resume_uses_one_replace_and_confirms_playing(runtime):
    run_resume(runtime)
    starts = [(d, a) for d, a, _ in runtime.calls if a in ("media_play", "play_media")]
    assert starts == [("media_player", "media_play"), ("music_assistant", "play_media")]
    assert runtime.coord._playback_health == "healthy"
    assert runtime.coord._log[0]["executed"] is True
    assert runtime.coord._playback_recovery_source == "tv_resume"


def test_already_playing_never_starts_twice(runtime):
    for state in runtime.states.values():
        state.state = "playing"
    run_resume(runtime)
    assert not any(a in ("media_play", "play_media") for _, a, _ in runtime.calls)
    assert runtime.coord._playback_health == "healthy"


def test_disabled_recovery_preserves_safety_cancellation_during_health_check(runtime):
    runtime.coord._opts[C.CONF_PLAYBACK_RECOVERY] = False
    for state in runtime.states.values():
        state.state = "playing"

    def stop_during_samples():
        if runtime.now > 3.0:
            runtime.inputs = replace(runtime.inputs, stop_latch=True)

    runtime.on_wait = stop_during_samples
    run_resume(runtime)
    assert runtime.coord._playback_recovery_stage == "cancelled"
    assert runtime.coord._playback_health == "inactive"
    assert runtime.coord._log[0]["executed"] is False


def test_successful_dispatch_still_idle_is_failed_and_not_periodically_retried(runtime):
    runtime.replace_plays = False
    run_resume(runtime)
    assert runtime.coord._playback_recovery_stage == "failed"
    assert runtime.coord._playback_health == "unhealthy"
    assert runtime.coord._log[0]["executed"] is False
    assert runtime.coord._log[0]["dispatched"] is True
    previous = len(runtime.calls)
    run_resume(runtime)
    assert len(runtime.calls) == previous


@pytest.mark.parametrize("change", [
    {"bio_state": "sleep", "bio_sleep": True}, {"bio_state": "provisional_sleep"},
    {"stop_latch": True}, {"away_gate": True}, {"action": C.ACTION_PAUSE},
    {"homepods_target": 0.0}, {"audio_owner": "tv_denon"}, {"manual_playback": True},
    {"volume_apply_allowed": False}, {"suppress_homepods_start": True},
])
def test_safety_change_during_settling_prevents_recovery(runtime, change):
    runtime.on_wait = lambda: setattr(runtime, "inputs", replace(runtime.inputs, **change))
    run_resume(runtime)
    assert not any(d == "music_assistant" for d, _, _ in runtime.calls)
    assert runtime.coord._playback_recovery_stage == "cancelled"
    assert runtime.coord._playback_health == "inactive"
    assert runtime.coord._log[0]["executed"] is False


def test_existing_owner_suppresses_resume_and_policy_start(runtime):
    async def run():
        runtime.coord._wake_start_owned = True
        await runtime.coord._execute(L.ApplyPlan(homepods_action=C.ACTION_RESUME, execute=True))
        await runtime.coord._execute(L.ApplyPlan(homepods_action=C.ACTION_START_RADIO, execute=True))
    asyncio.run(run())
    assert runtime.calls == []


def test_missing_station_fails_without_script_fallback(runtime):
    runtime.inputs = replace(runtime.inputs, radio_station="missing")
    run_resume(runtime)
    assert runtime.coord._playback_health_reason == "media_id_missing"
    assert not any(d in ("music_assistant", "script") for d, _, _ in runtime.calls)


def test_provider_failure_diagnostic_never_contains_exception_payload(runtime):
    runtime.provider_error = True
    run_resume(runtime)
    assert runtime.coord._playback_health_reason == "provider_dispatch_failed:ValueError"
    assert "token" not in str(runtime.coord._log)


def test_single_flight_remains_owned_while_waiting(runtime):
    runtime.on_wait = lambda: runtime.coord._schedule_resume_reconciliation(source="resume")
    run_resume(runtime)
    assert sum(d == "music_assistant" for d, _, _ in runtime.calls) == 1


def test_wake_cannot_replace_an_owned_resume_task(runtime):
    runtime.on_wait = runtime.coord._schedule_radio_autostart
    run_resume(runtime)
    assert sum(d == "music_assistant" for d, _, _ in runtime.calls) == 1
    assert runtime.coord._playback_health == "healthy"
    assert runtime.coord._playback_recovery_source == "tv_resume"


def test_wake_single_flight_and_unmute_survive_falling_resume_permission(runtime):
    for state in runtime.states.values():
        state.attributes["is_volume_muted"] = True

    async def run():
        runtime.coord._schedule_radio_autostart()
        await runtime.coord._playback_recovery_task

    asyncio.run(run())
    assert sum(d == "music_assistant" for d, _, _ in runtime.calls) == 1
    assert not any(a == "media_play" for _, a, _ in runtime.calls)
    assert runtime.inputs.homepods_resume_allowed is False
    assert all(state.attributes["is_volume_muted"] is False for state in runtime.states.values() if state is not runtime.states["media_player.group"])
    assert runtime.coord._playback_health == "healthy"


@pytest.mark.parametrize("media_id", ["", "unknown", "radiobrowser://", "radio://bad value", 4])
def test_invalid_owned_dispatch_does_not_call_provider_or_script(runtime, media_id):
    async def run():
        runtime.coord._wake_start_owned = True
        return await runtime.coord._dispatch_automatic_radio(media_id, source="tv_resume")
    assert asyncio.run(run()) is False
    assert runtime.calls == []
    assert runtime.coord._radio_dispatch_status()["last_error"] in ("media_id_missing", "media_id_invalid")
