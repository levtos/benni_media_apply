"""Radio provider errors use the existing single-flight soft recovery (#60)."""
import asyncio
from dataclasses import replace

import pytest
import bma_const as C
import bma_logic as L
from tests.test_resume_reconciliation import remember_content, run_resume

pytest_plugins = ["tests.test_resume_reconciliation"]


@pytest.mark.parametrize("source", ["wake", "policy", "resume", "tv_resume"])
def test_provider_failure_falls_back_once_in_same_owner(runtime, source):
    runtime.inputs = replace(runtime.inputs, radio_station="jack_fm_berlin")
    original = runtime.coord.hass.services.async_call

    async def service(domain, action, data, **kwargs):
        if action == "play_media" and data["media_id"].startswith("radiobrowser://"):
            runtime.calls.append((domain, action, data))
            # Even stale playing from another session must not hide this failure.
            for entity in ["media_player.group", "media_player.pod1", "media_player.pod2"]:
                runtime.states[entity].state = "playing"
            runtime.states["media_player.group"].attributes["media_content_id"] = "library://radio/unrelated"
            raise ValueError("station not found")
        assert runtime.coord._wake_start_owned
        return await original(domain, action, data, **kwargs)

    runtime.coord.hass.services.async_call = service

    async def run():
        if source == "wake":
            runtime.coord._schedule_radio_autostart()
        else:
            runtime.coord._schedule_resume_reconciliation(source=source)
        await runtime.coord._playback_recovery_task

    asyncio.run(run())
    starts = [data["media_id"] for _, action, data in runtime.calls if action == "play_media"]
    assert starts == [C.RADIO_CATALOG["jack_fm_berlin"], C.RADIO_FALLBACK_CATALOG["jack_fm_berlin"]]
    assert runtime.coord._playback_health == "healthy"
    # Existing counter includes the explicit unmute as well as the soft replace.
    assert runtime.coord._playback_recovery_attempts == 2
    assert not any(action == "media_play" for _, action, _ in runtime.calls)
    assert runtime.inputs.radio_station == "jack_fm_berlin"


def test_both_provider_failures_remain_failed_despite_playing(runtime):
    runtime.provider_error = True
    for entity in ["media_player.group", "media_player.pod1", "media_player.pod2"]:
        runtime.states[entity].state = "playing"
    # Begin idle to admit, then report the old session after dispatch fails.
    runtime.states["media_player.group"].state = "idle"
    runtime.on_wait = lambda: setattr(runtime.states["media_player.group"], "state", "playing")
    run_resume(runtime)
    assert runtime.coord._playback_recovery_stage == "failed"
    assert runtime.coord._playback_health == "unhealthy"
    assert runtime.coord._playback_health_reason == "provider_dispatch_failed:ValueError"
    assert sum(a == "play_media" for _, a, _ in runtime.calls) == 2


@pytest.mark.parametrize("change", [{"stop_latch": True}, {"manual_playback": True}, {"bio_state": "sleep", "bio_sleep": True}, {"audio_owner": "private_stack"}, {"homepods_target": 0}])
def test_gate_change_before_fallback_prevents_second_start(runtime, change):
    runtime.provider_error = True
    def change_gate():
        if "homepods_target" in change:
            # #41 permits a transient zero before first playback; after playback
            # a zero is a hard gate and must prevent the provider fallback.
            runtime.coord._playback_episode_seen_playing = True
        runtime.inputs = replace(runtime.inputs, **change)

    runtime.on_wait = change_gate
    run_resume(runtime)
    assert sum(a == "play_media" for _, a, _ in runtime.calls) == 1
    assert runtime.coord._playback_health != "healthy"


def test_content_mismatch_cannot_become_healthy(runtime):
    original = runtime.coord.hass.services.async_call

    async def service(domain, action, data, **kwargs):
        await original(domain, action, data, **kwargs)
        if action == "play_media":
            runtime.states["media_player.group"].attributes["media_content_id"] = "library://radio/unrelated"

    runtime.coord.hass.services.async_call = service
    run_resume(runtime)
    assert runtime.coord._playback_recovery_stage == "failed"
    assert runtime.coord._playback_health_reason == "radio_content_unconfirmed"
    assert sum(a == "play_media" for _, a, _ in runtime.calls) == 2


def test_shortcuts_refresh_rb_uri_keep_provider_preference_and_library_opaque(runtime, monkeypatch):
    monkeypatch.setattr(runtime.coord, "_ma_config_entry_id", lambda: "test")

    async def search(name, limit):
        if name != "GAYFM":
            return []
        return [
            {"name": name, "uri": "library://radio/123", "favorite": True},
            {"name": name, "uri": "tunein--example://radio/s96482"},
            {"name": name, "uri": "radiobrowser://radio/new-verified-result"},
            {"name": "GAYFM unrelated", "uri": "radiobrowser://radio/wrong"},
        ]

    monkeypatch.setattr(runtime.coord, "async_search_radio", search)
    shortcuts = asyncio.run(runtime.coord.async_radio_shortcuts())
    gayfm = next(row for row in shortcuts if row["key"] == "gayfm")
    assert gayfm["uri"] == "radiobrowser://radio/new-verified-result"
    assert gayfm["fallback_uri"] == "tunein--example://radio/s96482"
    assert "library://radio/123" in gayfm["content_ids"]
    assert shortcuts[0]["key"] == C.DEFAULT_RADIO_STATION
    assert L.radio_uri_identity(gayfm["fallback_uri"]) == C.RADIO_FALLBACK_CATALOG["gayfm"]


def test_catalog_has_six_matching_provider_pairs():
    assert set(C.RADIO_CATALOG) == set(C.RADIO_FALLBACK_CATALOG)
    assert len(C.RADIO_CATALOG) == 6
    assert L.radio_fallback_uri("library://radio/3") is None
    assert L.radio_fallback_uri(C.RADIO_CATALOG["wdr2_bergisches_land"]) == "tunein://radio/s99174"


def test_tv_resume_fallback_keeps_saved_station_instead_of_plan(runtime):
    remember_content(runtime, C.RADIO_CATALOG["jack_fm_berlin"], "radio")
    run_resume(runtime)
    starts = [(action, data.get("media_id")) for _, action, data in runtime.calls if action in ("media_play", "play_media")]
    assert starts == [("media_play", None), ("play_media", C.RADIO_FALLBACK_CATALOG["jack_fm_berlin"])]
    assert runtime.inputs.radio_station == "gayfm"
    assert runtime.coord._playback_health == "healthy"


def test_disabled_airplay_recovery_still_falls_back_for_provider_failure(runtime):
    runtime.coord._opts[C.CONF_PLAYBACK_RECOVERY] = False
    runtime.replace_plays = False
    run_resume(runtime)
    assert sum(action == "play_media" for _, action, _ in runtime.calls) == 2
    assert runtime.coord._playback_health == "unhealthy"


def test_search_timeout_retains_verified_catalog_and_only_searches_requested_station(runtime, monkeypatch):
    monkeypatch.setattr(runtime.coord, "_ma_config_entry_id", lambda: "test")
    queries = []
    async def search(name, limit):
        queries.append(name)
        raise TimeoutError
    monkeypatch.setattr(runtime.coord, "async_search_radio", search)
    rows = asyncio.run(runtime.coord.async_radio_shortcuts(station_key="jack_fm_berlin"))
    assert queries == ["Jack FM Deutschland"]
    assert [row["uri"] for row in rows] == [C.RADIO_CATALOG["jack_fm_berlin"], C.RADIO_FALLBACK_CATALOG["jack_fm_berlin"]]


def test_library_alias_is_not_rewritten_as_rb_and_is_accepted_as_same_station(runtime, monkeypatch):
    monkeypatch.setattr(runtime.coord, "_ma_config_entry_id", lambda: "test")
    async def search(name, limit):
        return [{"name": name, "uri": "library://radio/verified"}]
    monkeypatch.setattr(runtime.coord, "async_search_radio", search)
    rows = asyncio.run(runtime.coord.async_radio_shortcuts(station_key="jack_fm_berlin"))
    assert rows[0]["uri"] == C.RADIO_CATALOG["jack_fm_berlin"]
    assert runtime.coord._radio_content_matches("library://radio/verified", rows[0]["uri"])
    assert not runtime.coord._radio_content_matches("library://radio/other", rows[0]["uri"])


def test_saved_station_different_from_plan_accepts_its_library_fallback(runtime, monkeypatch):
    monkeypatch.setattr(runtime.coord, "_ma_config_entry_id", lambda: "test")
    async def search(name, limit):
        return [{"name": name, "uri": "library://radio/" + name.replace(" ", "-")}]
    monkeypatch.setattr(runtime.coord, "async_search_radio", search)
    remember_content(runtime, C.RADIO_CATALOG["jack_fm_berlin"], "music")
    original = runtime.coord.hass.services.async_call
    async def service(domain, action, data, **kwargs):
        await original(domain, action, data, **kwargs)
        if action == "play_media":
            runtime.states["media_player.group"].attributes["media_content_id"] = "library://radio/Jack-FM-Deutschland"
    runtime.coord.hass.services.async_call = service
    run_resume(runtime)
    assert runtime.coord._playback_health == "healthy"
    assert next(data["media_id"] for _, action, data in runtime.calls if action == "play_media") == C.RADIO_FALLBACK_CATALOG["jack_fm_berlin"]


def test_ambiguous_library_names_do_not_confirm_playback(runtime, monkeypatch):
    monkeypatch.setattr(runtime.coord, "_ma_config_entry_id", lambda: "test")
    async def search(name, limit):
        return [{"name": "Jack FM Deutschland", "uri": "library://radio/a"}, {"name": "Jack FM Deutschland", "uri": "library://radio/b"}]
    monkeypatch.setattr(runtime.coord, "async_search_radio", search)
    rows = asyncio.run(runtime.coord.async_radio_shortcuts(station_key="jack_fm_berlin"))
    assert rows[0]["content_ids"] == [C.RADIO_CATALOG["jack_fm_berlin"], C.RADIO_FALLBACK_CATALOG["jack_fm_berlin"]]


@pytest.mark.parametrize("uri", [None, "", "tunein", "malformed", 123])
def test_incomplete_content_id_does_not_crash_health(uri):
    assert L.radio_uri_identity(uri) == (uri if isinstance(uri, str) else None)
