"""R12 #46: chronological input and real coordinator side-effect regressions."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import bma_const as C
import bma_logic as L
from tests.test_resume_reconciliation import runtime  # noqa: F401


def step(state=None, device="appletv", tv="off", **kwargs):
    return L.decide_tv_wol(
        L.Inputs(media_device=device, tv_player_state=tv, **kwargs), state
    )


@pytest.mark.parametrize("device", ["tv", "appletv"])
def test_live_shutdown_race_without_prior_wol(device):
    _, state = step(device="none", tv="on")
    plan, state = step(state, device, "on")
    assert not plan.fire and not state.wol_available
    for value in (device, device, "denon"):
        plan, state = step(state, value, "off")
        assert not plan.fire


@pytest.mark.parametrize("device", ["tv", "appletv"])
def test_wake_on_off_then_new_episode(device):
    _, state = step(device="none")
    plan, state = step(state, device)
    assert plan.fire and state.pending and state.fired
    episode = state.episode_id
    for tv in ("off", "on", "off", "off"):
        plan, state = step(state, device, tv)
        assert not plan.fire and not state.wol_available
        assert state.episode_id == episode
    _, state = step(state, "denon")
    assert not state.episode_active
    plan, state = step(state, device)
    assert plan.fire and state.episode_id == episode + 1
    assert state.last_rearm_reason == "r12:new_screen_episode"
    assert not step(state, device)[0].fire


@pytest.mark.parametrize("device", ["none", "denon", "homepods", "pc", "ps5", "switch", None, "unknown", "unavailable", "invalid"])
def test_no_screen_never_wakes(device):
    plan, _ = step(device=device)
    assert not plan.fire


@pytest.mark.parametrize("tv", ["on", "off", "unknown", "unavailable"])
def test_reload_existing_screen_is_fail_closed(tv):
    _, state = step(tv=tv)
    for value in (tv, "off", "on", "off"):
        plan, state = step(state, tv=value)
        assert not plan.fire
    _, state = step(state, "none")
    assert step(state)[0].fire


@pytest.mark.parametrize("missing", [None, "unknown", "unavailable", "unexpected"])
def test_missing_intent_cannot_end_or_rearm_episode(missing):
    _, state = step(device="none")
    _, state = step(state)
    _, state = step(state, missing)
    assert state.episode_active and state.screen_intent is None
    assert not state.pending
    assert not step(state)[0].fire


def test_shutdown_edge_cannot_be_a_new_screen_request():
    _, state = step(device="none", tv="on")
    plan, state = step(state)
    assert not plan.fire
    assert state.last_consume_reason == "r12:tv_shutdown_edge"
    assert not step(state)[0].fire


def test_unknown_tv_does_not_hide_shutdown_edge():
    _, state = step(device="none", tv="on")
    _, state = step(state, tv="unknown")
    assert not step(state)[0].fire


def test_screen_device_switch_does_not_rearm():
    _, state = step(device="none")
    _, state = step(state, "appletv")
    assert not step(state, "tv")[0].fire


def test_new_episode_waits_for_known_tv_off():
    _, state = step(device="none", tv="unknown")
    plan, state = step(state, tv="unknown")
    assert not plan.fire and state.wol_available
    assert step(state)[0].fire


def prepare_runtime(env):
    env.inputs = replace(env.inputs, media_device="none", tv_player_state="off")
    env.coord.entry.options[C.CONF_TV_PLAYER] = "media_player.test_tv"
    env.coord.entry.options[C.CONF_TV_WOL_MAC] = "00:00:00:00:00:00"
    _, state = L.decide_tv_wol(env.inputs)
    env.inputs = replace(env.inputs, media_device="appletv")
    plan, env.coord._tv_wol_state = L.decide_tv_wol(env.inputs, state)
    assert plan.fire
    return env.coord._tv_wol_state.episode_id


def test_executor_exactly_once(runtime):  # noqa: F811
    episode = prepare_runtime(runtime)

    async def run():
        await asyncio.gather(*(runtime.coord._execute_tv_wol(episode) for _ in range(2)))

    asyncio.run(run())
    assert [action for _, action, _ in runtime.calls] == ["turn_on", "send_magic_packet"]


@pytest.mark.parametrize("change", ["shadow", "non_screen", "unknown", "tv_on", "sleep_off"])
def test_executor_rechecks_queued_permission(runtime, change):  # noqa: F811
    episode = prepare_runtime(runtime)
    if change == "shadow":
        runtime.coord.entry.options[C.CONF_APPLY_ENABLED] = False
    elif change == "sleep_off":
        runtime.coord._tv_wol_state.consume("r12:sleep_tv_off")
    elif change == "tv_on":
        runtime.inputs = replace(runtime.inputs, tv_player_state="on")
    else:
        runtime.inputs = replace(runtime.inputs, media_device="denon" if change == "non_screen" else None)
    asyncio.run(runtime.coord._execute_tv_wol(episode))
    assert runtime.calls == []
    assert runtime.coord._tv_wol_state.suppressed_reason


def test_old_task_cannot_consume_new_episode(runtime):  # noqa: F811
    old = prepare_runtime(runtime)
    _, state = step(runtime.coord._tv_wol_state, "none")
    _, runtime.coord._tv_wol_state = step(state)
    new = runtime.coord._tv_wol_state.episode_id

    async def run():
        await runtime.coord._execute_tv_wol(old)
        assert runtime.coord._tv_wol_state.pending
        await runtime.coord._execute_tv_wol(new)

    asyncio.run(run())
    assert len(runtime.calls) == 2


def test_mac_recheck_after_turn_on_yields(runtime, monkeypatch):  # noqa: F811
    episode = prepare_runtime(runtime)

    async def service(*args):
        runtime.calls.append(args)
        runtime.coord._tv_wol_state.consume("r12:sleep_tv_off")

    monkeypatch.setattr(runtime.coord, "_svc", service)
    asyncio.run(runtime.coord._execute_tv_wol(episode))
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("bio", ["sleep", "provisional_sleep"])
def test_real_sleep_off_consumes_before_service(runtime, monkeypatch, bio):  # noqa: F811
    prepare_runtime(runtime)
    runtime.inputs = replace(runtime.inputs, bio_state=bio, tv_power_on=True, tv_player_state="on")
    coord = runtime.coord
    coord._sleep_tv_state = L.SleepTvState(armed=True, deadline=1.0, warned_for_deadline=1.0)

    async def persist():
        pass

    async def service(domain, action, data):
        assert coord._tv_wol_state.last_consume_reason == "r12:sleep_tv_off"
        runtime.calls.append((domain, action, data))
        runtime.inputs = replace(runtime.inputs, tv_player_state="off", tv_power_on=False)
        plan, coord._tv_wol_state = L.decide_tv_wol(runtime.inputs, coord._tv_wol_state)
        assert not plan.fire

    monkeypatch.setattr(coord, "_async_persist_sleep_tv", persist)
    monkeypatch.setattr(coord, "_svc", service)
    monkeypatch.setattr(coord, "_compute", lambda: {})
    asyncio.run(coord._run_sleep_tv(1.0))
    assert [action for _, action, _ in runtime.calls] == ["turn_off"]
    runtime.inputs = replace(
        runtime.inputs, tv_player_state="on", tv_power_on=True
    )
    asyncio.run(coord._run_sleep_tv(1.0))
    assert [action for _, action, _ in runtime.calls] == ["turn_off"]


@pytest.mark.parametrize("shadow", [False, True])
def test_compute_live_sequence_and_shadow(runtime, monkeypatch, shadow):  # noqa: F811
    """Run actual admission/task scheduling across complete screen episodes."""
    coord = runtime.coord
    coord.entry.options[C.CONF_APPLY_ENABLED] = not shadow
    coord.entry.options[C.CONF_TV_PLAYER] = "media_player.test_tv"
    # Isolate other domains' side effects; R12 compute/scheduling/runner are real.
    for method in ("_schedule_execute", "_apply_nachlauf", "_dispatch_private_exit",
                   "_reconcile_sleep_tv_tasks", "_persist_sleep_tv", "_schedule_stuck_mute_recovery"):
        monkeypatch.setattr(coord, method, lambda *args, **kwargs: None)
    runtime.inputs = replace(runtime.inputs, action=C.ACTION_NONE,
                             media_device="none", tv_player_state="off")
    tasks = []

    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    runtime.coord.hass.async_create_task = create_task

    async def tick(device, tv):
        runtime.inputs = replace(runtime.inputs, media_device=device, tv_player_state=tv)
        coord._compute()
        if tasks:
            await asyncio.gather(*tasks)

    async def run():
        # Reload into a running screen, then the reported stale-input race.
        await tick("appletv", "on")
        await tick("appletv", "off")
        await tick("appletv", "off")
        assert runtime.calls == []
        await tick("denon", "off")
        await tick("appletv", "off")
        assert len(runtime.calls) == (0 if shadow else 1)
        await tick("appletv", "on")
        await tick("appletv", "off")
        assert len(runtime.calls) == (0 if shadow else 1)
        # Enabling Apply in the same shadow episode must not replay a wake.
        coord.entry.options[C.CONF_APPLY_ENABLED] = True
        await tick("appletv", "off")
        assert len(runtime.calls) == (0 if shadow else 1)
        await tick("none", "off")
        await tick("tv", "off")
        assert len(runtime.calls) == (1 if shadow else 2)

    asyncio.run(run())
    assert all(action == "turn_on" for _, action, _ in runtime.calls)
