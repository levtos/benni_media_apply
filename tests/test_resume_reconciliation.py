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
    for name in ("homeassistant", "homeassistant.components", "homeassistant.components.media_player", "homeassistant.config_entries", "homeassistant.core", "homeassistant.helpers", "homeassistant.helpers.event", "homeassistant.helpers.storage", "homeassistant.helpers.update_coordinator", "homeassistant.util", "homeassistant.util.dt"):
        modules[name] = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, modules[name])
    modules["homeassistant.components.media_player"].MediaPlayerEntityFeature = types.SimpleNamespace(VOLUME_MUTE=8)
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
    env = types.SimpleNamespace(now=1.0, calls=[], states={}, replace_plays=True, on_wait=None, provider_error=False, media_play_count=0, resume_plays_at=None, resume_target_after_play=None)
    env.inputs = L.Inputs(apply_enabled=True, volume_apply_allowed=True, action=C.ACTION_RESUME, homepods_should_pause=False, homepods_resume_allowed=True, homepods_target=0.3, quiet_mode=False, presence_state="anwesend", away_gate=False, stop_latch=False, radio_ready=True, manual_playback=False, planned_station_playing=False, bio_sleep=False, bio_state="awake", tv_power_on=False, audio_owner="homepods", radio_station="gayfm")
    group = "media_player.group"
    pods = ["media_player.pod1", "media_player.pod2"]
    for entity in [group, *pods]:
        env.states[entity] = types.SimpleNamespace(state="idle", attributes={"is_volume_muted": False, "supported_features": 8})

    async def service(domain, action, data, **kwargs):
        env.calls.append((domain, action, data))
        if domain == "media_player" and action == "media_pause":
            env.states[group].state = "paused"
        if domain == "media_player" and action == "media_play":
            env.media_play_count += 1
            if env.resume_plays_at is not None and env.media_play_count >= env.resume_plays_at:
                for state in env.states.values():
                    state.state = "playing"
                env.inputs = replace(
                    env.inputs,
                    homepods_resume_allowed=False,
                    action=C.ACTION_NONE,
                    audio_owner="homepods",
                    homepods_target=env.resume_target_after_play or env.inputs.homepods_target,
                )
        if domain == "music_assistant":
            if env.provider_error:
                raise ValueError("private-url?token=must-not-appear")
            if env.replace_plays:
                for state in env.states.values():
                    state.state = "playing"
                env.states[group].attributes["media_content_id"] = data["media_id"]
                env.states[group].attributes["media_content_type"] = data["media_type"]
                env.inputs = replace(env.inputs, homepods_resume_allowed=False, action=C.ACTION_NONE, planned_station_playing=True, audio_owner="homepods")
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


def run_action(env, action):
    async def run():
        plan = L.ApplyPlan(homepods_action=action, execute=True)
        env.coord._maybe_log(plan)
        await env.coord._execute(plan)
        task = env.coord._playback_recovery_task
        if task:
            await task
    asyncio.run(run())


def run_resume(env):
    run_action(env, C.ACTION_RESUME)


def remember_content(env, media_id=None, media_type=None):
    state = env.states["media_player.group"]
    state.state = "playing"
    if media_id is not None:
        state.attributes["media_content_id"] = media_id
    if media_type is not None:
        state.attributes["media_content_type"] = media_type

    async def pause():
        await env.coord._execute(
            L.ApplyPlan(homepods_action=C.ACTION_PAUSE, execute=True)
        )

    asyncio.run(pause())
    env.calls.clear()


def test_unknown_content_falls_back_to_planned_station_exactly_once(runtime):
    remember_content(runtime)
    runtime.inputs = replace(runtime.inputs, audio_owner="none")
    run_resume(runtime)
    starts = [(d, a) for d, a, _ in runtime.calls if a in ("media_play", "play_media")]
    assert starts == [("music_assistant", "play_media")]
    assert next(data["media_id"] for domain, action, data in runtime.calls if domain == "music_assistant" and action == "play_media") == L.resolve_radio_uri("gayfm")
    assert runtime.coord._playback_health == "healthy"
    assert runtime.coord._log[0]["executed"] is True
    assert runtime.coord._playback_recovery_source == "tv_resume"


def test_remembered_radio_retries_same_uri_not_planned_station(runtime):
    jack = "radiobrowser://station/jack"
    remember_content(runtime, jack, "radio")
    runtime.inputs = replace(runtime.inputs, audio_owner="none")
    run_resume(runtime)
    starts = [(d, a, data) for d, a, data in runtime.calls if a in ("media_play", "play_media")]
    assert [(d, a) for d, a, _ in starts] == [("media_player", "media_play"), ("music_assistant", "play_media")]
    assert starts[-1][2]["media_id"] == jack
    assert starts[-1][2]["media_id"] != L.resolve_radio_uri("gayfm")
    assert runtime.coord._playback_health == "healthy"


@pytest.mark.parametrize("media_type", ["album", "playlist"])
def test_remembered_album_or_playlist_retries_media_play(runtime, media_type):
    remember_content(runtime, f"library://{media_type}/42", media_type)
    runtime.inputs = replace(runtime.inputs, audio_owner="none")
    runtime.resume_plays_at = 2
    run_resume(runtime)
    starts = [(d, a) for d, a, _ in runtime.calls if a in ("media_play", "play_media")]
    assert starts == [("media_player", "media_play"), ("media_player", "media_play")]
    assert runtime.coord._playback_health == "healthy"


def test_target_zero_allows_owned_resume_then_ramp_after_playing(runtime):
    remember_content(runtime, "radiobrowser://station/jack", "radio")
    runtime.inputs = replace(
        runtime.inputs, homepods_target=0.0, manual_playback=True, audio_owner="none"
    )
    runtime.resume_plays_at = 1
    runtime.resume_target_after_play = 0.4
    run_resume(runtime)
    assert [(d, a) for d, a, _ in runtime.calls if a in ("media_play", "play_media")] == [("media_player", "media_play")]
    assert not any(action == "volume_set" for _, action, _ in runtime.calls)
    assert runtime.coord._playback_health == "healthy"
    ramp = L.decide_apply(
        replace(
            runtime.inputs,
            homepods_state="playing",
            homepods_volume=0.1,
            homepods_configured=True,
            manual_playback=False,
        ),
        L.ApplyState(),
        L.RampSettings(),
    )[0]
    assert ramp.homepods_levels
    assert ramp.homepods_levels[-1] == 0.4


def test_live_owner_none_allows_resume_target_positive(runtime):
    remember_content(runtime, "radiobrowser://station/jack", "radio")
    runtime.inputs = replace(runtime.inputs, audio_owner="none", homepods_target=0.5)
    runtime.resume_plays_at = 1
    run_resume(runtime)
    assert [(d, a) for d, a, _ in runtime.calls if a in ("media_play", "play_media")] == [
        ("media_player", "media_play")
    ]
    assert runtime.coord._playback_health == "healthy"
    assert runtime.inputs.homepods_resume_allowed is False


def test_policy_start_radio_owner_none_starts_once(runtime):
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_START_RADIO,
        audio_owner="none",
        homepods_resume_allowed=False,
    )
    run_action(runtime, C.ACTION_START_RADIO)
    assert sum(domain == "music_assistant" for domain, _, _ in runtime.calls) == 1
    assert runtime.coord._playback_health == "healthy"


def test_wake_episode_ignores_resume_allowed_after_dispatch(runtime):
    runtime.inputs = replace(runtime.inputs, audio_owner="none")
    def check_after_start():
        runtime.coord._compute()
        runtime.on_wait = None

    runtime.on_wait = check_after_start

    async def run():
        runtime.coord._schedule_radio_autostart()
        task = runtime.coord._playback_recovery_task
        if task:
            await task

    asyncio.run(run())
    assert runtime.coord._playback_health == "healthy"
    assert runtime.inputs.homepods_resume_allowed is False


def test_manual_end_trigger_b_starts_with_owner_none(runtime):
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_NONE,
        audio_owner="none",
        homepods_resume_allowed=True,
        manual_playback=False,
    )

    async def run():
        runtime.coord._schedule_radio_resume()
        delayed = runtime.coord._radio_resume_task
        if delayed:
            await delayed
        recovery = runtime.coord._playback_recovery_task
        if recovery:
            await recovery

    asyncio.run(run())
    assert sum(domain == "music_assistant" for domain, _, _ in runtime.calls) == 1
    assert runtime.coord._playback_health == "healthy"


def test_manual_flag_keeps_owned_content_until_new_user_content(runtime):
    jack = "radiobrowser://station/jack"
    remember_content(runtime, jack, "radio")
    runtime.inputs = replace(runtime.inputs, manual_playback=True)
    runtime.resume_plays_at = 1
    run_resume(runtime)
    assert runtime.coord._playback_health == "healthy"

    remember_content(runtime, jack, "radio")
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_RESUME,
        homepods_resume_allowed=True,
        manual_playback=True,
        planned_station_playing=False,
    )
    runtime.media_play_count = 0
    waits = 0

    def user_change_after_owned_sample():
        nonlocal waits
        waits += 1
        if waits >= 2:
            runtime.states["media_player.group"].attributes["media_content_id"] = "radiobrowser://station/user-choice"

    runtime.on_wait = user_change_after_owned_sample
    run_resume(runtime)
    assert runtime.coord._playback_recovery_stage == "cancelled"
    assert runtime.coord._playback_health_reason == "resume_content_changed"
    assert [(d, a) for d, a, _ in runtime.calls if a in ("media_play", "play_media")] == [("media_player", "media_play")]


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
    {"audio_owner": "tv_denon"}, {"audio_owner": "private_stack"},
    {"audio_owner": "unknown"}, {"manual_playback": True},
    {"volume_apply_allowed": False}, {"suppress_homepods_start": True},
])
def test_safety_gate_prevents_start_retry_and_volume(runtime, change):
    runtime.inputs = replace(runtime.inputs, **change)
    run_resume(runtime)
    assert not any(a in ("media_play", "play_media") for _, a, _ in runtime.calls)
    assert not any(a == "volume_set" for _, a, _ in runtime.calls)
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


@pytest.mark.parametrize("attributes", [{"supported_features": 0}, {"supported_features": 8}])
def test_playing_pods_without_mute_control_are_healthy_and_not_unmuted(runtime, attributes):
    for state in runtime.states.values():
        state.state = "playing"
    for entity in ("media_player.pod1", "media_player.pod2"):
        runtime.states[entity].attributes = dict(attributes)

    run_resume(runtime)

    assert runtime.coord._playback_health == "healthy"
    assert runtime.coord._playback_recovery_stage == "healthy"
    assert not any(action == "volume_mute" for _, action, _ in runtime.calls)
    assert not any(domain == "music_assistant" for domain, _, _ in runtime.calls)


def test_unavailable_pod_without_mute_control_remains_unhealthy(runtime):
    for state in runtime.states.values():
        state.state = "playing"
    runtime.states["media_player.pod2"].state = "unavailable"
    runtime.states["media_player.pod2"].attributes = {"supported_features": 0}

    health = runtime.coord._playback_health_snapshot()

    assert health == L.PlaybackHealth("unhealthy", "pod_2_unavailable")


@pytest.mark.parametrize("media_id", ["", "unknown", "radiobrowser://", "radio://bad value", 4])
def test_invalid_owned_dispatch_does_not_call_provider_or_script(runtime, media_id):
    async def run():
        runtime.coord._wake_start_owned = True
        return await runtime.coord._dispatch_automatic_radio(media_id, source="tv_resume")
    assert asyncio.run(run()) is False
    assert runtime.calls == []
    assert runtime.coord._radio_dispatch_status()["last_error"] in ("media_id_missing", "media_id_invalid")
