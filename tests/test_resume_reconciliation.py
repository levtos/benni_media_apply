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
    env.bio_entity = "sensor.bio_state"
    env.latch_entity = "input_boolean.media_stop_latch"
    for entity in [group, *pods]:
        env.states[entity] = types.SimpleNamespace(state="idle", attributes={"is_volume_muted": False, "supported_features": 8})
    env.states[env.bio_entity] = types.SimpleNamespace(state="awake", attributes={})
    env.states[env.latch_entity] = types.SimpleNamespace(state="off", attributes={})

    async def service(domain, action, data, **kwargs):
        env.calls.append((domain, action, data))
        if domain == "media_player" and action == "media_pause":
            env.states[group].state = "paused"
        if domain == "media_player" and action == "media_play":
            env.media_play_count += 1
            if env.resume_plays_at is not None and env.media_play_count >= env.resume_plays_at:
                for entity in [group, *pods]:
                    env.states[entity].state = "playing"
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
                for entity in [group, *pods]:
                    env.states[entity].state = "playing"
                env.states[group].attributes["media_content_id"] = data["media_id"]
                env.states[group].attributes["media_content_type"] = data["media_type"]
                env.inputs = replace(env.inputs, homepods_resume_allowed=False, action=C.ACTION_NONE, planned_station_playing=True, audio_owner="homepods")
        if action == "volume_mute":
            for entity in data["entity_id"]:
                env.states[entity].attributes["is_volume_muted"] = False
        if domain == "homeassistant" and action == "turn_off" and data["entity_id"] == env.latch_entity:
            env.states[env.latch_entity].state = "off"
            env.inputs = replace(env.inputs, stop_latch=False)

    hass = types.SimpleNamespace(states=types.SimpleNamespace(get=env.states.get), services=types.SimpleNamespace(async_call=service), loop=types.SimpleNamespace(time=lambda: env.now), async_create_task=asyncio.create_task)
    options = {C.CONF_APPLY_ENABLED: True, C.CONF_HOMEPODS_PLAYER: group, C.CONF_HOMEPODS_PODS: pods, C.CONF_BIO_STATE: env.bio_entity, C.CONF_STOP_LATCH: env.latch_entity}
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


def set_bio_latch(runtime, *, previous, current, latch=True, **changes):
    runtime.coord._last_bio_state = previous
    runtime.states[runtime.bio_entity].state = current
    runtime.states[runtime.latch_entity].state = "on" if latch else "off"
    runtime.inputs = replace(
        runtime.inputs,
        bio_state=current,
        bio_sleep=current == "sleep",
        stop_latch=latch,
        **changes,
    )


async def settle_edge_tasks(runtime):
    await asyncio.sleep(0)
    tasks = [
        task
        for task in (
            runtime.coord._wake_task,
            runtime.coord._playback_recovery_task,
        )
        if task is not None and not task.done()
    ]
    if tasks:
        await asyncio.gather(*tasks)
    await asyncio.sleep(0)


def test_stop_latch_sleep_to_waking_resets_once_and_wake_becomes_healthy(runtime):
    set_bio_latch(
        runtime,
        previous="sleep",
        current="waking",
        audio_owner="none",
        action=C.ACTION_NONE,
    )

    async def run():
        runtime.coord._compute()
        await settle_edge_tasks(runtime)

    asyncio.run(run())
    clears = [
        call
        for call in runtime.calls
        if call[:2] == ("homeassistant", "turn_off")
    ]
    assert len(clears) == 1
    assert sum(domain == "music_assistant" for domain, _, _ in runtime.calls) == 1
    assert runtime.coord._playback_health == "healthy"
    assert runtime.coord.status()["wake"]["stop_latch_reset_reason"] == "bio_wake_edge"


def test_stop_latch_sleep_to_awake_resets_when_waking_is_skipped(runtime):
    set_bio_latch(
        runtime,
        previous="sleep",
        current="awake",
        action=C.ACTION_NONE,
        away_gate=True,
    )

    async def run():
        runtime.coord._compute()
        await settle_edge_tasks(runtime)

    asyncio.run(run())
    assert [
        call for call in runtime.calls if call[:2] == ("homeassistant", "turn_off")
    ] == [
        (
            "homeassistant",
            "turn_off",
            {"entity_id": runtime.latch_entity},
        )
    ]


@pytest.mark.parametrize(
    "block",
    [
        {"away_gate": True},
        {"audio_owner": "tv_denon", "tv_power_on": True},
    ],
)
def test_bio_wake_edge_resets_latch_even_when_start_is_blocked(runtime, block):
    set_bio_latch(
        runtime,
        previous="provisional_sleep",
        current="waking",
        action=C.ACTION_NONE,
        **block,
    )

    async def run():
        runtime.coord._compute()
        await settle_edge_tasks(runtime)

    asyncio.run(run())
    assert sum(action == "turn_off" for _, action, _ in runtime.calls) == 1
    assert not any(domain == "music_assistant" for domain, _, _ in runtime.calls)


@pytest.mark.parametrize(
    ("previous", "current"),
    [("awake", "awake"), (None, "waking")],
)
def test_no_stop_latch_reset_without_known_sleep_to_wake_edge(
    runtime, previous, current
):
    set_bio_latch(
        runtime,
        previous=previous,
        current=current,
        action=C.ACTION_NONE,
        away_gate=True,
    )

    async def run():
        runtime.coord._compute()
        await asyncio.sleep(0)

    asyncio.run(run())
    assert not any(action == "turn_off" for _, action, _ in runtime.calls)
    assert runtime.states[runtime.latch_entity].state == "on"


def test_shadow_consumes_bio_wake_edge_without_resetting_latch(runtime):
    runtime.coord.entry.options[C.CONF_APPLY_ENABLED] = False
    set_bio_latch(
        runtime,
        previous="sleep",
        current="waking",
        action=C.ACTION_NONE,
    )

    async def run():
        runtime.coord._compute()
        await asyncio.sleep(0)
        runtime.coord.entry.options[C.CONF_APPLY_ENABLED] = True
        runtime.coord._compute()
        await asyncio.sleep(0)

    asyncio.run(run())
    assert not any(action == "turn_off" for _, action, _ in runtime.calls)
    assert runtime.states[runtime.latch_entity].state == "on"


@pytest.mark.parametrize("source", ["policy", "tv_resume", "resume", "retry"])
def test_stop_latch_remains_hard_gate_for_non_wake_sources(runtime, source):
    runtime.inputs = replace(runtime.inputs, stop_latch=True)
    runtime.states[runtime.latch_entity].state = "on"

    async def run():
        if source == "policy":
            await runtime.coord._execute(
                L.ApplyPlan(homepods_action=C.ACTION_START_RADIO, execute=True)
            )
        elif source in ("tv_resume", "resume"):
            runtime.coord._schedule_resume_reconciliation(source=source)
            if runtime.coord._playback_recovery_task is not None:
                await runtime.coord._playback_recovery_task
        else:
            runtime.coord._wake_start_owned = True
            runtime.coord._playback_episode_admitted = True
            runtime.coord._playback_recovery_source = "resume"
            await runtime.coord._dispatch_automatic_radio(
                L.resolve_radio_uri("gayfm"),
                source="resume_retry",
                replace_existing=True,
            )

    asyncio.run(run())
    assert runtime.states[runtime.latch_entity].state == "on"
    assert not any(
        action in ("media_play", "play_media", "turn_off")
        for _, action, _ in runtime.calls
    )


def test_wake_admission_ignores_existing_stop_latch_only_once(runtime):
    runtime.inputs = replace(runtime.inputs, stop_latch=True)

    assert (
        runtime.coord._playback_start_block_reason(
            runtime.inputs,
            source="wake",
            admission=True,
        )
        is None
    )
    assert (
        runtime.coord._playback_start_block_reason(
            runtime.inputs,
            source="wake",
            admission=False,
        )
        == "stop_latch"
    )


def test_new_stop_during_running_wake_episode_cancels(runtime):
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_NONE,
        audio_owner="none",
    )

    def stop_after_dispatch():
        if runtime.states["media_player.group"].state != "playing":
            return
        runtime.inputs = replace(runtime.inputs, stop_latch=True)
        runtime.states[runtime.latch_entity].state = "on"
        runtime.coord._compute()
        runtime.on_wait = None

    runtime.on_wait = stop_after_dispatch

    async def run():
        runtime.coord._schedule_radio_autostart()
        await runtime.coord._playback_recovery_task

    asyncio.run(run())
    assert runtime.coord._playback_recovery_stage == "cancelled"
    assert runtime.coord._playback_health_reason == "stop_latch"
    assert runtime.states[runtime.latch_entity].state == "on"
    assert not any(action == "turn_off" for _, action, _ in runtime.calls)


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


def test_wake_admission_and_episode_ignore_resume_allowed(runtime):
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_NONE,
        audio_owner="none",
        homepods_resume_allowed=False,
    )
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
    assert sum(domain == "music_assistant" for domain, _, _ in runtime.calls) == 1
    assert runtime.coord._playback_health == "healthy"
    assert runtime.inputs.homepods_resume_allowed is False


def test_live_wake_policy_and_quiet_sequence_has_one_start(runtime):
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_NONE,
        audio_owner="none",
        homepods_resume_allowed=False,
    )
    policy_arrived = False
    quiet_ended = False

    def advance_live_sequence():
        nonlocal policy_arrived, quiet_ended
        if not policy_arrived:
            policy_arrived = True
            runtime.inputs = replace(
                runtime.inputs,
                action=C.ACTION_START_RADIO,
                quiet_mode=True,
            )
            runtime.coord._compute()
        elif not quiet_ended and runtime.now >= 5.0:
            quiet_ended = True
            runtime.inputs = replace(runtime.inputs, quiet_mode=False)
            runtime.coord._compute()

    runtime.on_wait = advance_live_sequence

    async def run():
        runtime.coord._schedule_radio_autostart()
        task = runtime.coord._playback_recovery_task
        assert task is not None
        await task

    asyncio.run(run())
    assert policy_arrived is True
    assert quiet_ended is True
    assert sum(domain == "music_assistant" for domain, _, _ in runtime.calls) == 1
    assert runtime.coord._playback_health == "healthy"


@pytest.mark.parametrize(("group_state", "seen_playing"), [("idle", False), ("playing", True)])
def test_quiet_does_not_end_owned_start_episode(runtime, group_state, seen_playing):
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_NONE,
        quiet_mode=True,
        homepods_resume_allowed=False,
    )
    runtime.states["media_player.group"].state = group_state
    runtime.coord._wake_start_owned = True
    runtime.coord._playback_episode_admitted = True
    runtime.coord._playback_episode_seen_playing = seen_playing
    runtime.coord._playback_recovery_source = "wake"
    runtime.coord._playback_recovery_stage = "initial_start"

    async def run():
        runtime.coord._compute()
        await asyncio.sleep(0)

    asyncio.run(run())
    assert runtime.coord._wake_start_owned is True
    assert runtime.coord._playback_recovery_stage == "initial_start"


@pytest.mark.parametrize(
    ("blocked", "cleared"),
    [
        ({"homepods_resume_allowed": False}, {"homepods_resume_allowed": True}),
        ({"volume_apply_allowed": False}, {"volume_apply_allowed": True}),
        ({"presence_state": "unknown"}, {"presence_state": "anwesend"}),
        ({"radio_ready": False}, {"radio_ready": True}),
        ({"audio_owner": "unknown"}, {"audio_owner": "homepods"}),
    ],
)
def test_policy_readmission_once_per_transient_blocker_clearance(
    runtime, monkeypatch, blocked, cleared
):
    attempts = []

    def schedule(*, source, readmission=False):
        attempts.append((source, readmission, runtime.now))
        runtime.coord._policy_readmission_blocker = None
        runtime.coord._policy_readmission_last_attempt_at = runtime.now

    monkeypatch.setattr(runtime.coord, "_schedule_resume_reconciliation", schedule)
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_START_RADIO,
        **blocked,
    )
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)
    runtime.inputs = replace(runtime.inputs, **cleared)
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)

    assert attempts == [("policy", True, 1.0)]


def test_state_change_rechecks_persistent_policy_after_presence_hold(runtime):
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_START_RADIO,
        presence_state="unknown",
    )

    async def run():
        runtime.coord._on_state_change(None)
        assert runtime.coord._policy_readmission_blocker == "presence_unknown"
        runtime.inputs = replace(runtime.inputs, presence_state="anwesend")
        runtime.coord._on_state_change(None)
        task = runtime.coord._playback_recovery_task
        assert task is not None
        await task

    asyncio.run(run())
    assert sum(domain == "music_assistant" for domain, _, _ in runtime.calls) == 1
    assert runtime.coord._playback_health == "healthy"


def test_policy_readmission_keeps_thirty_second_attempt_distance(runtime, monkeypatch):
    attempts = []

    def schedule(*, source, readmission=False):
        attempts.append((source, readmission, runtime.now))
        runtime.coord._policy_readmission_blocker = None
        runtime.coord._policy_readmission_last_attempt_at = runtime.now

    monkeypatch.setattr(runtime.coord, "_schedule_resume_reconciliation", schedule)
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_START_RADIO,
        presence_state="unknown",
    )
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)
    runtime.inputs = replace(runtime.inputs, presence_state="anwesend")
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)
    runtime.inputs = replace(runtime.inputs, radio_ready=False)
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)
    runtime.now = 30.9
    runtime.inputs = replace(runtime.inputs, radio_ready=True)
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)
    runtime.now = 31.0
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)

    assert attempts == [("policy", True, 1.0), ("policy", True, 31.0)]


@pytest.mark.parametrize(
    "hard_block",
    [
        {"stop_latch": True},
        {"away_gate": True},
        {"bio_sleep": True, "bio_state": "sleep"},
        {"bio_sleep": False, "bio_state": "provisional_sleep"},
        {"homepods_should_pause": True},
        {"audio_owner": "tv_denon", "tv_power_on": True},
        {"manual_playback": True},
    ],
)
def test_policy_readmission_never_rearms_from_hard_blocker(
    runtime, monkeypatch, hard_block
):
    attempts = []
    monkeypatch.setattr(
        runtime.coord,
        "_schedule_resume_reconciliation",
        lambda **kwargs: attempts.append(kwargs),
    )
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_START_RADIO,
        presence_state="unknown",
    )
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)
    runtime.inputs = replace(
        runtime.inputs,
        presence_state="anwesend",
        **hard_block,
    )
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)
    cleared = {
        key: (
            "awake"
            if key == "bio_state"
            else "homepods"
            if key == "audio_owner"
            else False
        )
        for key in hard_block
    }
    runtime.inputs = replace(runtime.inputs, **cleared)
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)

    assert attempts == []


def test_policy_readmission_does_not_restart_failed_or_user_stopped_episode(
    runtime, monkeypatch
):
    attempts = []
    monkeypatch.setattr(
        runtime.coord,
        "_schedule_resume_reconciliation",
        lambda **kwargs: attempts.append(kwargs),
    )
    runtime.inputs = replace(
        runtime.inputs,
        action=C.ACTION_START_RADIO,
        presence_state="unknown",
    )
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)
    runtime.inputs = replace(runtime.inputs, presence_state="anwesend")
    runtime.coord._playback_recovery_stage = "failed"
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)
    runtime.coord._playback_recovery_stage = "cancelled"
    runtime.coord._playback_health_reason = "resume_user_stopped"
    runtime.coord._policy_readmission_blocker = None
    runtime.coord._maybe_schedule_policy_readmission(runtime.inputs)

    assert attempts == []


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
    for entity in ("media_player.group", "media_player.pod1", "media_player.pod2"):
        runtime.states[entity].attributes["is_volume_muted"] = True

    async def run():
        runtime.coord._schedule_radio_autostart()
        await runtime.coord._playback_recovery_task

    asyncio.run(run())
    assert sum(d == "music_assistant" for d, _, _ in runtime.calls) == 1
    assert not any(a == "media_play" for _, a, _ in runtime.calls)
    assert runtime.inputs.homepods_resume_allowed is False
    assert all(
        runtime.states[entity].attributes["is_volume_muted"] is False
        for entity in ("media_player.pod1", "media_player.pod2")
    )
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
