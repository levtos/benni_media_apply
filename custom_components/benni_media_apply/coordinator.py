"""Media-Apply-Coordinator (Single-Instance, event-driven Executor).

DataUpdateCoordinator ohne Polling: rechnet bei State-Changes der gebundenen
Quell-Entities den Apply-Plan neu (logic.decide_apply) und FÜHRT ihn aus —
idempotent, mit abbrechbarem HomePods-Ramp-Task (16×1s; Quiet bricht durch).

Apply-Gate: `apply_enabled` (Option, Shadow-Kill-Switch) × `volume_apply_allowed`
(pro Entscheidung, aus media_policy). Im Shadow wird der Plan berechnet + als
Status-/Debug-Sensoren exponiert, aber NICHT ausgeführt.

start_radio wird (Phase 1) an ein Script delegiert (Radio-Katalog bleibt YAML).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import logic
from .const import (
    ACTION_NONE,
    ACTION_DENON_OFF,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_START_RADIO,
    BIO_AWAKE_VALUES,
    BIO_SLEEP_CONTEXT_VALUES,
    BIO_SLEEP_VALUE,
    CONF_ACTION,
    CONF_APPLY_ENABLED,
    CONF_AWAY_GATE,
    CONF_BIO_STATE,
    CONF_DEBOUNCE_MAX_WAIT,
    CONF_DEBOUNCE_SECONDS,
    CONF_DENON_IMMEDIATE,
    CONF_DENON_NACHLAUF_PC,
    CONF_DENON_NACHLAUF_TV,
    CONF_PRIVATE_EXIT_DELAY,
    DEFAULT_PRIVATE_EXIT_DELAY,
    AUDIO_OWNER_PRIVATE,
    CONF_AUDIO_OWNER,
    CONF_DENON_PLAYER,
    CONF_DENON_POWER,
    CONF_DUCKED_LEVEL,
    CONF_HOMEPODS_PLAYER,
    CONF_HOMEPODS_PODS,
    CONF_HOMEPODS_RESUME_ALLOWED,
    CONF_HOMEPODS_SHOULD_PAUSE,
    CONF_MANUAL_PLAYBACK,
    CONF_MEDIA_DEVICE,
    CONF_MUSIC_ASSISTANT_APP_ID,
    CONF_MUSIC_ASSISTANT_RESTART_COOLDOWN,
    CONF_MUSIC_ASSISTANT_RESTART_WAIT,
    CONF_PC_POWER,
    CONF_PLAYBACK_HARD_RECOVERY,
    CONF_PLAYBACK_RECOVERY,
    CONF_PLAYBACK_RECOVERY_HARD_AFTER,
    CONF_PLAYBACK_RECOVERY_SETTLE,
    CONF_PLANNED_STATION_PLAYING,
    CONF_PRESENCE_STATE,
    CONF_PROFILE,
    CONF_QUIET_MODE,
    CONF_RADIO_AUTOSTART,
    CONF_RADIO_RESUME_DELAY,
    CONF_SLEEP_TV_EXTEND,
    CONF_SLEEP_TV_NOTIFY,
    CONF_SLEEP_TV_OFF_DELAY,
    CONF_SLEEP_TV_WARN_LEAD,
    CONF_SLEEP_TV_WARN_MESSAGE,
    CONF_RADIO_READY,
    CONF_RADIO_START_SCRIPT,
    CONF_RADIO_STATION,
    CONF_RAMP_STEP_DELAY,
    CONF_RAMP_STEPS,
    CONF_STOP_LATCH,
    CONF_SUBWOOFER_ALLOWED,
    CONF_SUBWOOFER_SWITCH,
    CONF_TINY_DELTA,
    CONF_TV_PLAYER,
    CONF_TV_POWER,
    CONF_TV_WOL_MAC,
    CONF_WAKE_DEBOUNCE,
    CONF_WAKE_PLAY_LEAD,
    CONF_WAKE_START_VOLUME,
    CONF_WAKE_TRIGGERS,
    CONF_VOL_TARGET_DENON,
    CONF_VOL_TARGET_HOMEPODS,
    CONF_VOLUME_APPLY_ALLOWED,
    DEFAULT_APPLY_ENABLED,
    DEFAULT_DEBOUNCE_MAX_WAIT,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_DENON_IMMEDIATE,
    DEFAULT_DENON_NACHLAUF_PC,
    DEFAULT_DENON_NACHLAUF_TV,
    DEFAULT_DUCKED_LEVEL,
    DEFAULT_MUSIC_ASSISTANT_APP_ID,
    DEFAULT_MUSIC_ASSISTANT_RESTART_COOLDOWN,
    DEFAULT_MUSIC_ASSISTANT_RESTART_WAIT,
    DEFAULT_PLAYBACK_HARD_RECOVERY,
    DEFAULT_PLAYBACK_HEALTH_SAMPLE_INTERVAL,
    DEFAULT_PLAYBACK_HEALTH_SAMPLES,
    DEFAULT_PLAYBACK_RECOVERY,
    DEFAULT_PLAYBACK_RECOVERY_HARD_AFTER,
    DEFAULT_PLAYBACK_RECOVERY_RECHECK,
    DEFAULT_PLAYBACK_RECOVERY_SETTLE,
    DEFAULT_STUCK_MUTE_BACKSTOP_INTERVAL,
    DEFAULT_STUCK_MUTE_EVENT_COOLDOWN,
    DEFAULT_STUCK_MUTE_RETRY_ATTEMPTS,
    DEFAULT_STUCK_MUTE_RETRY_DELAY,
    DEFAULT_PROFILE,
    DEFAULT_RADIO_AUTOSTART,
    DEFAULT_RADIO_RESUME_DELAY,
    DEFAULT_RADIO_SEARCH_LIMIT,
    DEFAULT_RADIO_START_SCRIPT,
    DEFAULT_SLEEP_TV_NOTIFY,
    DEFAULT_SLEEP_TV_OFF_CONFIRM,
    DEFAULT_SLEEP_TV_OFF_DELAY,
    DEFAULT_SLEEP_TV_WARN_LEAD,
    DEFAULT_SLEEP_TV_WARN_MESSAGE,
    DEFAULT_TV_WOL_MAC,
    DEFAULT_WAKE_DEBOUNCE,
    DEFAULT_WAKE_PLAY_LEAD,
    DEFAULT_WAKE_START_VOLUME,
    DEFAULT_RAMP_STEP_DELAY,
    DEFAULT_RAMP_STEPS,
    DEFAULT_TINY_DELTA,
    DENON_CONSUMER_DEVICES,
    DOMAIN,
    EXEC_IMMEDIATE,
    EXEC_SHADOW,
    PLAYER_OFF_VALUES,
    PLAYER_PLAYING_VALUES,
    RADIO_ENQUEUE,
    RADIO_MEDIA_TYPE,
    SCREEN_DEVICES,
    PROFILE_PREFILL,
    PROFILES,
    WATCH_KEYS,
    SLEEP_TV_EVIDENCE_CONTRACT_VERSION,
    SLEEP_TV_STORAGE_VERSION,
    sleep_tv_storage_key,
)

_LOGGER = logging.getLogger(__name__)

_TRUE = frozenset({"on", "true", "1", "home", "active", "playing", "open"})


def _bool(s: str | None) -> bool:
    return s is not None and s.lower() in _TRUE


class MediaApplyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Eine Instanz pro Config-Entry (Single-Instance-Modell)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.entry = entry
        profile = entry.data.get(CONF_PROFILE, DEFAULT_PROFILE)
        self._profile = profile if profile in PROFILES else DEFAULT_PROFILE
        self._unsub_state = None
        self._ramp_task = None
        self._ramp_active = False
        # R2/R3 — Debounce-Fenster + serialisierte Ausführung (latest-wins).
        self._debounce_unsub = None
        self._debounce_deadline: Optional[float] = None   # loop.time(), für remaining_s
        # benni_media#13: Startzeit des laufenden Fensters (loop.time()) für den
        # Anti-Starvation-Deckel — bleibt über ein Re-Arm hinweg erhalten.
        self._debounce_started_at: Optional[float] = None
        self._pending_plan: Optional[logic.ApplyPlan] = None
        # Cockpit-Regeländerungen: serverseitiger latest-wins Reapply. Der
        # Browser darf geschlossen oder neu geladen werden, ohne den Timer zu
        # verlieren oder einen zweiten anzulegen.
        self._reapply_unsub = None
        self._reapply_deadline: Optional[float] = None
        self._reapply_delay_s = 30.0
        self._reapply_reason: str | None = None
        self._exec_lock = asyncio.Lock()
        self._apply_state = logic.ApplyState()
        self._nachlauf_state = logic.NachlaufState()
        self._private_exit_state = logic.PrivateExitState()   # control#3
        self._tv_wol_state = logic.TvWolState()
        self._sleep_tv_state = logic.SleepTvState()
        self._sleep_tv_store: Store[dict[str, Any]] = Store(
            hass,
            SLEEP_TV_STORAGE_VERSION,
            sleep_tv_storage_key(entry.entry_id),
        )
        self._sleep_tv_task: Optional[asyncio.Task] = None
        self._sleep_tv_task_deadline: float | None = None
        self._sleep_tv_confirmation_task: Optional[asyncio.Task] = None
        self._sleep_tv_confirmation_deadline: float | None = None
        self._sleep_tv_last_saved: dict[str, Any] | None = None
        self._sleep_tv_save_lock = asyncio.Lock()
        self._last_extend_state: str | None = None
        self._wake_task: Optional[asyncio.Task] = None
        self._last_wake_states: dict[str, bool] = {}
        self._last_bio_state: str | None = None
        self._last_stop_latch_reset_reason: str | None = None
        self._last_stop_latch_reset_at: str | None = None
        self._radio_resume_task: Optional[asyncio.Task] = None
        self._radio_dispatch_state = logic.RadioDispatchState()
        self._radio_dispatch_payload: dict[str, Any] = {}
        # #41 — genau ein Besitzer für Wake-Start + gestufte Recovery.
        self._playback_recovery_task: Optional[asyncio.Task] = None
        self._stuck_mute_task: Optional[asyncio.Task] = None
        self._stuck_mute_backstop_unsub = None
        self._unsub_pod_state = None
        self._last_unmute_attempt_at: float | None = None
        self._stuck_mute_retry_not_before = 0.0
        self._playback_recovery_source: str | None = None
        self._wake_start_owned = False
        self._playback_action_log: dict[str, Any] | None = None
        self._resume_episode_finished = False
        self._resume_content_id: str | None = None
        self._resume_content_type: str | None = None
        self._resume_content_player: str | None = None
        self._resume_episode_expected_content_id: str | None = None
        self._resume_episode_seen_playing = False
        self._resume_episode_dispatched = False
        self._playback_episode_admitted = False
        self._playback_episode_seen_playing = False
        self._playback_health = "idle"
        self._playback_health_reason: str | None = None
        self._playback_recovery_stage = "idle"
        self._playback_recovery_attempts = 0
        self._playback_recovery_started_at: float | None = None
        self._last_hard_recovery_at: float | None = None
        self._last_manual_playback: bool | None = None
        self._nachlauf_tasks: dict[str, asyncio.Task] = {}
        self._last_debug: dict[str, Any] = {}
        # Observability (FLEET-46): Ramp-Fortschritt + Apply-Log-Ringpuffer.
        self._ramp_step = 0
        self._ramp_total = 0
        self._log: deque[dict[str, Any]] = deque(maxlen=20)
        self._last_log_sig: tuple | None = None
        self._radio_shortcuts_cache: list[dict[str, Any]] | None = None

    async def async_load_stored(self) -> None:
        """Load Issue #59's restart-safe absolute deadlines before first refresh."""

        raw = await self._sleep_tv_store.async_load()
        self._sleep_tv_state = logic.SleepTvState.from_dict(raw)
        self._sleep_tv_last_saved = self._sleep_tv_state.as_dict()

    def _persist_sleep_tv(self) -> None:
        if self._sleep_tv_state.as_dict() == self._sleep_tv_last_saved:
            return
        self.hass.async_create_task(self._async_persist_sleep_tv())

    async def _async_persist_sleep_tv(self) -> None:
        """Serialize Store writes and make critical marker writes awaitable."""

        async with self._sleep_tv_save_lock:
            raw = self._sleep_tv_state.as_dict()
            if raw == self._sleep_tv_last_saved:
                return
            await self._sleep_tv_store.async_save(raw)
            self._sleep_tv_last_saved = dict(raw)

    # ----- profile / binding -----
    @property
    def profile(self) -> str:
        return self._profile

    @property
    def _opts(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def apply_enabled(self) -> bool:
        return bool(self._opts.get(CONF_APPLY_ENABLED, DEFAULT_APPLY_ENABLED))

    @property
    def _radio_autostart_enabled(self) -> bool:
        return bool(self._opts.get(CONF_RADIO_AUTOSTART, DEFAULT_RADIO_AUTOSTART))

    @property
    def _playback_recovery_enabled(self) -> bool:
        return bool(self._opts.get(CONF_PLAYBACK_RECOVERY, DEFAULT_PLAYBACK_RECOVERY))

    def _entity_id(self, key: str) -> Any:
        """Auto-Bind (core_state-Blaupause): options ▶ data ▶ PROFILE_PREFILL."""
        return (
            self.entry.options.get(key)
            or self.entry.data.get(key)
            or PROFILE_PREFILL.get(self._profile, {}).get(key)
        )

    def _homepods_volume_targets(self) -> list[str]:
        """benni_media#16 — Volume geht PRO POD (Lastenheft: kein Gruppen-Call).

        Ein `volume_set` auf die AirPlay-Sync-GRUPPE weckt einen pausierten Verbund
        wieder auf (belegter Live-Bug, 15:28); auf die einzelnen Pods nicht. Pause/
        Resume/Radio bleiben auf der Gruppe (nur ein Player-Call), nur der Volume-
        Set/Ramp adressiert die Pods. Sind keine Pods gebunden, Fallback auf die
        Gruppe (non-regressiv gegenüber v0.18.x)."""
        return logic.volume_target_entities(
            self._entity_id(CONF_HOMEPODS_PODS),
            self._entity_id(CONF_HOMEPODS_PLAYER),
        )

    def _watched_entities(self) -> list[str]:
        ids: list[str] = []
        for key in WATCH_KEYS:
            val = self._entity_id(key)
            if isinstance(val, str) and val:
                ids.append(val)
            elif isinstance(val, (list, tuple)):   # Multi-Entity (Wake-Trigger)
                ids.extend(e for e in val if isinstance(e, str) and e)
        return list(dict.fromkeys(ids))

    def bindings(self) -> dict[str, Any]:
        return {key: self._entity_id(key) for key in WATCH_KEYS}

    def settings(self) -> logic.RampSettings:
        def _f(key: str, default: float) -> float:
            try:
                return float(self._opts.get(key, default))
            except (TypeError, ValueError):
                return default

        def _i(key: str, default: int) -> int:
            try:
                return int(self._opts.get(key, default))
            except (TypeError, ValueError):
                return default

        return logic.RampSettings(
            ramp_steps=_i(CONF_RAMP_STEPS, DEFAULT_RAMP_STEPS),
            ramp_step_delay_s=_f(CONF_RAMP_STEP_DELAY, DEFAULT_RAMP_STEP_DELAY),
            tiny_delta=_f(CONF_TINY_DELTA, DEFAULT_TINY_DELTA),
            ducked_level=_f(CONF_DUCKED_LEVEL, DEFAULT_DUCKED_LEVEL),
            debounce_seconds=_f(CONF_DEBOUNCE_SECONDS, DEFAULT_DEBOUNCE_SECONDS),
            debounce_max_wait_s=_f(CONF_DEBOUNCE_MAX_WAIT, DEFAULT_DEBOUNCE_MAX_WAIT),
            denon_immediate=bool(
                self._opts.get(CONF_DENON_IMMEDIATE, DEFAULT_DENON_IMMEDIATE)
            ),
            wake_start_volume=_f(CONF_WAKE_START_VOLUME, DEFAULT_WAKE_START_VOLUME),
            wake_debounce_seconds=_f(CONF_WAKE_DEBOUNCE, DEFAULT_WAKE_DEBOUNCE),
        )

    # ----- lifecycle -----
    @callback
    def async_start(self) -> None:
        watched = self._watched_entities()
        if watched:
            self._unsub_state = async_track_state_change_event(
                self.hass, watched, self._on_state_change
            )
            self.entry.async_on_unload(self._unsub_state)
        pods = self._homepods_volume_targets()
        if pods:
            self._unsub_pod_state = async_track_state_change_event(
                self.hass, pods, self._on_pod_state_change
            )
            self.entry.async_on_unload(self._unsub_pod_state)
        self._stuck_mute_backstop_unsub = async_track_time_interval(
            self.hass,
            self._on_stuck_mute_backstop,
            timedelta(seconds=DEFAULT_STUCK_MUTE_BACKSTOP_INTERVAL),
        )
        self.entry.async_on_unload(self._stuck_mute_backstop_unsub)

    @callback
    def _on_state_change(self, _event: Event) -> None:
        self.async_set_updated_data(self._compute())

    @callback
    def _on_pod_state_change(self, _event: Event) -> None:
        """Observe pod mute/state changes without re-running volume/ramp planning."""

        self._schedule_stuck_mute_recovery(source="event")

    @callback
    def _on_stuck_mute_backstop(self, _now: datetime) -> None:
        """Thirty-minute unmute-only safety net; never starts or changes volume."""

        self._schedule_stuck_mute_recovery(source="backstop")

    @callback
    def async_shutdown_ramp(self) -> None:
        """Unload-Hook: laufende Ramp-, Debounce- und Nachlauf-Tasks abbrechen."""
        self._cancel_ramp()
        self._cancel_debounce()
        self._cancel_reapply_timer()
        self._cancel_sleep_tv()
        self._cancel_sleep_tv_confirmation()
        self._cancel_wake()
        self._cancel_radio_resume()
        self._cancel_playback_recovery("unload")
        self._cancel_stuck_mute_recovery()
        for key in list(self._nachlauf_tasks):
            self._cancel_nachlauf(key)

    # ----- reads -----
    def _state(self, key: str) -> str | None:
        eid = self._entity_id(key)
        if not eid:
            return None
        st = self.hass.states.get(eid)
        if st is None or st.state in ("unknown", "unavailable"):
            return None
        return st.state

    def _attr_float(self, key: str, attr: str) -> Optional[float]:
        eid = self._entity_id(key)
        if not eid:
            return None
        st = self.hass.states.get(eid)
        if st is None:
            return None
        try:
            return float(st.attributes.get(attr))
        except (TypeError, ValueError):
            return None

    def _attr(self, key: str, attr: str) -> Any:
        eid = self._entity_id(key)
        st = self.hass.states.get(eid) if eid else None
        return st.attributes.get(attr) if st is not None else None

    def _float(self, key: str) -> Optional[float]:
        raw = self._state(key)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _tri_bool(self, key: str) -> Optional[bool]:
        """Tri-state: None wenn ungebunden ODER Zustand unbekannt/unavailable,
        sonst bool. Verhindert, dass Nachlauf-Timer auf fehlenden Daten armen."""
        if not self._entity_id(key):
            return None
        raw = self._state(key)   # None bei unknown/unavailable
        if raw is None:
            return None
        return _bool(raw)

    def _powered(self, key: str) -> Optional[bool]:
        """Power-Wahrheit eines core_devices-Geräts: bevorzugt Attribute
        (`powered`, `is_active`, `watt_active`) und fällt dann auf den
        bool-kompatiblen State zurück. None = ungebunden/
        unbekannt (FLEET-80: verhindert falsche Nachlauf-Arms auf „idle")."""
        eid = self._entity_id(key)
        if not eid:
            return None
        st = self.hass.states.get(eid)
        if st is None or st.state in ("unknown", "unavailable"):
            return None
        for attr in ("powered", "is_active", "watt_active"):
            value = st.attributes.get(attr)
            if isinstance(value, bool):
                return value
            if value is not None:
                return _bool(str(value))
        return _bool(st.state)

    def _denon_consumer_active(self) -> Optional[bool]:
        """FLEET-80 Cross-Source-Gate: Ist ein Denon-Konsument (media_device ∈
        DENON_CONSUMER_DEVICES) aktiv? `denon`/`homepods`/`none` zählen NICHT.
        None wenn media_device ungebunden/unbekannt ⇒ konservativ (kein Off)."""
        if not self._entity_id(CONF_MEDIA_DEVICE):
            return None
        md = self._state(CONF_MEDIA_DEVICE)   # None bei unknown/unavailable
        if md is None:
            return None
        return md in DENON_CONSUMER_DEVICES

    def _denon_consumer_holds(self) -> Optional[bool]:
        """benni_media#14: Konsumenten-Gate inkl. Power-Gegenprobe (pure Regel in
        `logic.denon_consumer_holds`). Wird auch beim Timer-Ablauf benutzt, damit
        die Ablauf-Gegenprobe dieselbe Wahrheit nutzt wie die Arm-Entscheidung —
        sonst armt der Timer, und ein stale media_device verhindert am Ende doch
        den Off."""
        return logic.denon_consumer_holds(self._build_inputs())

    def _denon_power_on(self) -> Optional[bool]:
        """Denon-Power: dediziertes Atomic bevorzugt (CONF_DENON_POWER, sobald
        nach #54 gebunden), sonst Ableitung aus dem bereits gebundenen
        Denon-media_player (state nicht in off/standby)."""
        if self._entity_id(CONF_DENON_POWER):
            return self._tri_bool(CONF_DENON_POWER)
        st = self._state(CONF_DENON_PLAYER)
        if st is None:
            return None
        return st not in PLAYER_OFF_VALUES

    def _bio_sleep(self) -> Optional[bool]:
        """bio_state == 'sleep' (core_state). None wenn ungebunden/unbekannt."""
        if not self._entity_id(CONF_BIO_STATE):
            return None
        st = self._state(CONF_BIO_STATE)
        if st is None:
            return None
        return st == BIO_SLEEP_VALUE

    # ----- evaluation -----
    def _build_inputs(self) -> logic.Inputs:
        return logic.Inputs(
            apply_enabled=self.apply_enabled,
            volume_apply_allowed=_bool(self._state(CONF_VOLUME_APPLY_ALLOWED)),
            action=self._state(CONF_ACTION) or "none",
            homepods_should_pause=_bool(self._state(CONF_HOMEPODS_SHOULD_PAUSE)),
            homepods_resume_allowed=_bool(self._state(CONF_HOMEPODS_RESUME_ALLOWED)),
            homepods_target=self._float(CONF_VOL_TARGET_HOMEPODS),
            denon_target=self._float(CONF_VOL_TARGET_DENON),
            subwoofer_allowed=_bool(self._state(CONF_SUBWOOFER_ALLOWED)),
            quiet_mode=_bool(self._state(CONF_QUIET_MODE)),
            presence_state=self._state(CONF_PRESENCE_STATE),
            presence_degraded=bool(self._entity_id(CONF_PRESENCE_STATE))
            and self._state(CONF_PRESENCE_STATE) is None,
            away_gate=self._tri_bool(CONF_AWAY_GATE),
            stop_latch=_bool(self._state(CONF_STOP_LATCH)),
            radio_station=self._state(CONF_RADIO_STATION),
            radio_ready=self._tri_bool(CONF_RADIO_READY),
            manual_playback=self._tri_bool(CONF_MANUAL_PLAYBACK),
            planned_station_playing=self._tri_bool(CONF_PLANNED_STATION_PLAYING),
            homepods_configured=bool(self._entity_id(CONF_HOMEPODS_PLAYER)),
            homepods_state=self._state(CONF_HOMEPODS_PLAYER),
            homepods_volume=self._attr_float(CONF_HOMEPODS_PLAYER, "volume_level"),
            denon_configured=bool(self._entity_id(CONF_DENON_PLAYER)),
            denon_state=self._state(CONF_DENON_PLAYER),
            denon_volume=self._attr_float(CONF_DENON_PLAYER, "volume_level"),
            subwoofer_configured=bool(self._entity_id(CONF_SUBWOOFER_SWITCH)),
            subwoofer_state=self._state(CONF_SUBWOOFER_SWITCH),
            # Phase 3 (R13/R14): watt-primäres `powered`-Attribut (FLEET-80) statt
            # State-String — „idle" bei OLED-Watt-Dip darf nicht als aus zählen.
            pc_power_on=self._powered(CONF_PC_POWER),
            tv_power_on=self._powered(CONF_TV_POWER),
            denon_power_on=self._denon_power_on(),
            bio_sleep=self._bio_sleep(),
            bio_state=self._state(CONF_BIO_STATE),
            sleep_source=self._attr(CONF_BIO_STATE, "sleep_source"),
            sleep_reference_start=self._attr(
                CONF_BIO_STATE, "sleep_reference_start"
            ),
            # FLEET-80 Cross-Source-Gate: anderer Denon-Konsument aktiv?
            denon_consumer_active=self._denon_consumer_active(),
            # Phase 4c (R12 TV-WoL).
            media_device=self._state(CONF_MEDIA_DEVICE),
            tv_player_state=self._state(CONF_TV_PLAYER),
            # control#3: Private Time aus dem media_policy audio_owner-Sensor.
            private_active=self._state(CONF_AUDIO_OWNER) == AUDIO_OWNER_PRIVATE,
            # benni_media#16: roher Owner fürs generalisierte Musikstart-Gate.
            audio_owner=self._state(CONF_AUDIO_OWNER),
        )

    def _compute(self, *, force_execute: bool = False) -> dict[str, Any]:
        inputs = self._build_inputs()
        media_blocked = logic.media_block_reason(inputs) is not None
        if self._playback_recovery_stage == "initial_start":
            if self._playback_episode_admitted and self._episode_group_has_played():
                recovery_block = self._playback_repair_block_reason(
                    inputs,
                    managed_episode=self._wake_start_owned,
                    require_positive_target=True,
                )
            else:
                recovery_block = self._playback_start_block_reason(
                    inputs,
                    source=self._playback_recovery_source or "wake",
                    admission=not self._playback_episode_admitted,
                )
        else:
            recovery_block = self._playback_repair_block_reason(
                inputs,
                managed_episode=self._wake_start_owned,
                require_positive_target=False,
            )
        if self._wake_start_owned and recovery_block is not None:
            self._cancel_playback_recovery(recovery_block)
        if media_blocked:
            self._cancel_radio_resume()
            self._cancel_wake()
        # benni_media#16: Hat ein Bildschirm-Stack (TV) das Audio übernommen, wird
        # ein noch WARTENDER verzögerter Radio-Resume sofort abgebrochen — sonst
        # feuert ein in einem kurzen (Flacker-)Musikfenster geplanter Start später
        # unter laufendem TV (belegter Folgefehler 22:18). Der Re-Check in
        # should_autostart_radio fängt es zusätzlich am Fire-Zeitpunkt ab.
        elif logic.screen_blocks_music_start(inputs):
            self._cancel_radio_resume()
        # control#3: Private-Exit-Denon-Off-Delay VOR decide_apply — der
        # suppress-Flag sperrt den HomePod-Start, solange der Delay läuft.
        pxplan, self._private_exit_state = logic.decide_private_exit(
            inputs, self._private_exit_state
        )
        if pxplan.suppress_homepods:
            inputs = replace(inputs, suppress_homepods_start=True)
        plan, self._apply_state = logic.decide_apply(
            inputs, self._apply_state, self.settings()
        )
        # (Der frühere 30 s-Startup-Guard gegen Radio-Restart ist entfallen: die
        # Musik-Baseline in media_policy ist jetzt debounced — sie emittiert im
        # Restore-Flap gar kein start_radio mehr, also nichts zu unterdrücken.)
        nplan, self._nachlauf_state = logic.decide_denon_nachlauf(
            inputs, self._nachlauf_state
        )
        twol, self._tv_wol_state = logic.decide_tv_wol(inputs, self._tv_wol_state)
        previous_bio_state = self._last_bio_state
        bio_to_awake, _ = self._bio_edges()   # sleep-Edge nur noch für media_state relevant
        self._schedule_stop_latch_reset(
            previous_bio_state in BIO_SLEEP_CONTEXT_VALUES and bio_to_awake,
            inputs,
        )
        edge_inp = replace(
            inputs,
            sleep_tv_extend_pressed=self._consume_extend_edge(),
            wake_trigger_fired=self._wake_trigger_fired(bio_to_awake),
        )
        now_epoch = dt_util.utcnow().timestamp()
        splan, self._sleep_tv_state = logic.decide_sleep_tv(
            edge_inp,
            self._sleep_tv_state,
            now=now_epoch,
            delay_s=self._duration(
                CONF_SLEEP_TV_OFF_DELAY, DEFAULT_SLEEP_TV_OFF_DELAY
            ),
            confirm_s=DEFAULT_SLEEP_TV_OFF_CONFIRM,
        )
        wplan = logic.decide_wake(edge_inp)
        wake_radio_start = bool(
            wplan.fire
            and not self._wake_start_owned
            and self.apply_enabled
            and self._radio_autostart_enabled
            and logic.should_autostart_radio(inputs)
        )
        if wake_radio_start:
            self._wake_start_owned = True
            self._playback_health = "settling"
            self._playback_health_reason = None
            self._playback_recovery_stage = "initial_start"
            self._playback_recovery_attempts = 0
        # Music Assistant play_media ist der einzige Start-Owner der Wake-Episode.
        # Policy-resume/start_radio darf währenddessen weder davor noch nach einem
        # AirPlay-State-Flap einen parallelen Startimpuls erzeugen.
        plan = logic.suppress_parallel_wake_start(plan, self._wake_start_owned)
        self._last_debug = {
            **plan.as_dict(), "nachlauf": nplan.as_dict(),
            "tv_wol": twol.as_dict(), "sleep_tv": splan.as_dict(),
            "wake": wplan.as_dict(), "private_exit": pxplan.as_dict(),
        }
        # benni_media#13: effektive Ziele festhalten, BEVOR `_schedule_execute`
        # den Denon-Set zur Sofort-Ausführung aus dem Plan entnimmt.
        effective_hp = (
            plan.homepods_levels[-1] if plan.homepods_levels else inputs.homepods_target
        )
        effective_dn = (
            plan.denon_set if plan.denon_set is not None else inputs.denon_target
        )
        self._maybe_log(plan)
        # control#3: Private-Exit-Denon-Off-Delay (eigener Timer, abbrechbar).
        # Flanken IMMER verarbeiten (Buchwerk auch im Shadow); realer Off gegatet.
        self._dispatch_private_exit(pxplan.timer)
        # R2/R3: Ausführung läuft über Debounce-Fenster + Serialisierung, Quiet
        # bricht sofort durch. Preview/Status (oben) aktualisieren sich pro Event.
        self._schedule_execute(plan, force=force_execute)
        # Nachlauf-Flanken IMMER verarbeiten (Arm/Cancel-Buchwerk auch im Shadow,
        # für Observability); der reale Denon-Off ist in _run_nachlauf gegatet.
        if nplan.active:
            self._apply_nachlauf(nplan)
        # R12 TV-WoL: SOFORT (kein Debounce), aber apply-gated (automatische Aktion).
        if twol.fire and self.apply_enabled:
            self.hass.async_create_task(
                self._execute_tv_wol(self._tv_wol_state.episode_id)
            )
        elif twol.fire:
            self._tv_wol_state.consume("r12:shadow")
            self._tv_wol_state.suppressed_reason = "r12:shadow"
        # Issue #59: reconcile persisted absolute deadlines instead of starting
        # relative RAM timers on every arm/extension.
        self._reconcile_sleep_tv_tasks()
        self._persist_sleep_tv()
        # R23 Wake-Sequenz: Trigger-Flanke → HomePods 0.10 → Debounce → Ramp auf Ziel.
        if wplan.fire and self.apply_enabled:
            self._schedule_wake()
        # FLEET-79 Radio-Autostart (Port der disabled YAML-Automationen).
        manual_off = self._manual_off_edge()
        if self.apply_enabled and self._radio_autostart_enabled and not media_blocked:
            if wake_radio_start:
                # Trigger A: Wake → geplante Station starten. Der Latch-Reset gehört
                # unabhängig davon ausschließlich der Bio-Wachflanke.
                self._schedule_radio_autostart()
            elif (
                not self._wake_start_owned
                and manual_off
                and inputs.action != ACTION_PAUSE
                and logic.should_autostart_radio(inputs)
            ):
                # Trigger B: manuelle Wiedergabe endete → nach Delay fortsetzen.
                self._schedule_radio_resume()
        self._schedule_stuck_mute_recovery(source="event")
        # FLEET-44/98: der manuelle private_time-Latch + seine Auto-Löschung
        # leben jetzt nativ in media_state (switch-Entität) — apply verwaltet
        # ihn nicht mehr.
        # benni_media#13 — Target-Sensoren zeigen das EFFEKTIVE Ziel, nicht das
        # Plan-Delta. Bisher stand hier nur der Plan: ist Ist == Soll (idempotenter
        # No-Op), war die Liste leer bzw. `denon_set` None → Sensor `unknown`,
        # obwohl die Policy ein gültiges Ziel liefert. Genau das zeigte die
        # Evidenz („Apply target becomes unknown") — ein Anzeige-Artefakt, keine
        # echte Invalidierung. `unknown` bleibt nur, wenn es wirklich KEIN Ziel gibt.
        return {
            "last_action": plan.homepods_action,
            "homepods_target": effective_hp,
            "denon_target": effective_dn,
            "ramp_active": self._ramp_active,
            "apply_enabled": self.apply_enabled,
            "execute": plan.execute,
            "denon_nachlauf_active": (
                self._nachlauf_state.pc_armed or self._nachlauf_state.tv_armed
            ),
            "playback_health": self._playback_health,
            "playback_health_attrs": self._playback_health_attrs(),
            "playback_recovery_stage": self._playback_recovery_stage,
            "sleep_tv_evidence": splan.evidence,
            "sleep_tv_evidence_attrs": self._sleep_tv_evidence_attrs(
                splan.evidence
            ),
        }

    async def _async_update_data(self) -> dict[str, Any]:
        return self._compute()

    # ----- R2/R3: Debounce + serialisierte Ausführung -----
    @callback
    def _schedule_execute(self, plan: "logic.ApplyPlan", *, force: bool = False) -> None:
        """Leitet einen Plan in die Ausführung (R2/R3). Quiet bricht sofort durch,
        sonst sammelt ein Debounce-Fenster Trigger-Bursts zu EINER Aktion."""
        mode = logic.execution_mode(plan)
        # Ein geplanter Regel-Reapply hält normale Zieländerungen zurück. Quiet
        # bleibt als sicherheitsrelevanter Hard-Override sofort wirksam.
        if self._reapply_unsub is not None and mode != EXEC_IMMEDIATE and not force:
            return
        if mode == EXEC_SHADOW:
            # Apply (wieder) aus → kein Pending mehr ausführen.
            self._cancel_debounce()
            self._pending_plan = None
            return
        if mode == EXEC_IMMEDIATE or force:
            # Quiet: laufendes Fenster verwerfen, sofort (serialisiert) ducken.
            self._cancel_debounce()
            self._pending_plan = plan
            self.hass.async_create_task(self._execute_serialized())
            return
        # benni_media#13 — Der Denon fährt keine Rampe: sein harter Volume-Set geht
        # SOFORT raus, sobald Zielkontext und Zielwert feststehen, und wartet nicht
        # das R2-Fenster ab. Die HomePods-Rampe bleibt bewusst unangetastet und
        # läuft weiter über das Debounce-Fenster.
        denon_now = logic.take_immediate_denon(plan, self.settings().denon_immediate)
        if denon_now is not None:
            self.hass.async_create_task(self._execute_denon_volume(denon_now))
        # EXEC_DEBOUNCE — R2/R3-Pending-Buchführung (pure entschieden, FLEET-245):
        # triviale Re-Evals stoßen das Fenster nicht neu an (Anti-Starvation),
        # aktualisieren aber den gepufferten Plan, damit keine überholte Aktion
        # ausgeführt wird.
        update_pending, restart = logic.debounce_decision(
            plan,
            self._debounce_unsub is not None,
            window_age_s=self._debounce_age(),
            max_wait_s=self.settings().debounce_max_wait_s,
        )
        if update_pending:
            self._pending_plan = plan
        if restart:
            self._start_debounce()

    def _debounce_age(self) -> Optional[float]:
        """Alter des laufenden R2-Fensters in Sekunden (benni_media#13)."""
        if self._debounce_started_at is None:
            return None
        return max(0.0, self.hass.loop.time() - self._debounce_started_at)

    @callback
    def _start_debounce(self) -> None:
        # Startzeit über das Re-Arm hinweg halten — sonst setzt jeder Burst-Trigger
        # das Alter zurück und der Anti-Starvation-Deckel könnte nie greifen.
        started = self._debounce_started_at
        self._cancel_debounce()
        window = self.settings().debounce_seconds
        now = self.hass.loop.time()
        self._debounce_started_at = started if started is not None else now
        self._debounce_deadline = now + window
        self._debounce_unsub = async_call_later(self.hass, window, self._fire_debounce)

    @callback
    def _cancel_debounce(self) -> None:
        if self._debounce_unsub is not None:
            self._debounce_unsub()
            self._debounce_unsub = None
        self._debounce_deadline = None
        # Fenster verworfen → Alter zurücksetzen. `_start_debounce` sichert die
        # Startzeit vorher und stellt sie beim Re-Arm wieder her (#13).
        self._debounce_started_at = None

    @callback
    def _fire_debounce(self, _now) -> None:
        self._debounce_unsub = None
        self._debounce_deadline = None
        self._debounce_started_at = None
        self.hass.async_create_task(self._execute_serialized())

    def _debounce_remaining(self) -> Optional[float]:
        """Restzeit bis das Fenster feuert (Sekunden), None wenn kein Fenster läuft."""
        if self._debounce_deadline is None:
            return None
        return round(max(0.0, self._debounce_deadline - self.hass.loop.time()), 2)

    def _reapply_remaining(self) -> Optional[float]:
        if self._reapply_deadline is None:
            return None
        return round(max(0.0, self._reapply_deadline - self.hass.loop.time()), 2)

    @callback
    def _cancel_reapply_timer(self) -> None:
        if self._reapply_unsub is not None:
            self._reapply_unsub()
            self._reapply_unsub = None
        self._reapply_deadline = None

    @callback
    def async_schedule_reapply(self, delay_s: float = 30.0, reason: str | None = None) -> dict[str, Any]:
        """Plane einen serverseitigen, latest-wins Regel-Reapply."""
        self._cancel_reapply_timer()
        self._cancel_debounce()
        self._pending_plan = None
        self._reapply_delay_s = max(0.0, float(delay_s))
        self._reapply_reason = reason or "rules_saved"
        self._reapply_deadline = self.hass.loop.time() + self._reapply_delay_s
        self._reapply_unsub = async_call_later(
            self.hass, self._reapply_delay_s, self._fire_reapply
        )
        self.async_update_listeners()
        return self.status()

    @callback
    def _fire_reapply(self, _now) -> None:
        self._reapply_unsub = None
        self._reapply_deadline = None
        # Erst beim Feuern neu rechnen: Kontext und Policy-Ziele sind damit
        # aktuell, kein beim Speichern eingefrorener Plan wird ausgeführt.
        self.async_set_updated_data(self._compute(force_execute=True))

    @callback
    def async_apply_reapply_now(self) -> dict[str, Any]:
        self._cancel_reapply_timer()
        self.async_set_updated_data(self._compute(force_execute=True))
        return self.status()

    @callback
    def async_cancel_reapply(self) -> dict[str, Any]:
        self._cancel_reapply_timer()
        self._reapply_reason = None
        self.async_update_listeners()
        return self.status()

    async def _execute_denon_volume(self, level: float) -> None:
        """benni_media#13: einzelner harter Denon-Volume-Set, am R2-Fenster vorbei.

        Läuft über dasselbe `_exec_lock` wie der normale Pfad — der Sofort-Set
        darf sich nicht mit einer laufenden Ausführung überschneiden.
        """
        denon = self._entity_id(CONF_DENON_PLAYER)
        if not denon:
            return
        async with self._exec_lock:
            await self._svc(
                "media_player", "volume_set",
                {"entity_id": denon, "volume_level": level},
            )
        _LOGGER.debug("media_apply: Denon-Sofort-Volume %s → %.3f", denon, level)

    async def _execute_serialized(self) -> None:
        """Serialisiert die Geräte-Schaltung (R3: Queue statt Race). Es läuft
        immer der zuletzt gepufferte Plan (idempotent → latest-wins); ein zweiter
        wartender Task findet None vor und ist ein No-op."""
        async with self._exec_lock:
            plan = self._pending_plan
            self._pending_plan = None
            if plan is None:
                return
            await self._execute(plan)

    def _maybe_log(self, plan: "logic.ApplyPlan") -> None:
        """Apply-Log-Ringpuffer: jede nicht-triviale Plan-Änderung mit Timestamp +
        execute-Flag (Shadow-Entscheidungen inklusive, für Observability)."""
        hp_target = plan.homepods_levels[-1] if plan.homepods_levels else None
        trivial = (
            plan.homepods_action == ACTION_NONE
            and not plan.homepods_levels
            and plan.denon_set is None
            and plan.subwoofer_set is None
            and not plan.quiet_override
        )
        sig = (plan.homepods_action, hp_target, plan.denon_set, plan.subwoofer_set,
               plan.quiet_override, plan.execute)
        if trivial or sig == self._last_log_sig:
            return
        self._last_log_sig = sig
        self._log.appendleft({
            "ts": dt_util.utcnow().isoformat(),
            "action": plan.homepods_action,
            "homepods_target": hp_target,
            "denon_target": plan.denon_set,
            "subwoofer_set": plan.subwoofer_set,
            "quiet": plan.quiet_override,
            "execution_requested": plan.execute,
            "executed": plan.execute and plan.homepods_action not in (ACTION_RESUME, ACTION_START_RADIO),
        })

    def status(self) -> dict[str, Any]:
        """Konsolidierter Apply-Status für Panel/Umbrella (WS-Contract = das
        Bleibende). Read-only: nur ein frischer Inputs-Snapshot, keine Neuberechnung
        des Plans (der kommt aus dem letzten Tick)."""
        inp = self._build_inputs()
        s = self.settings()
        plan = {k: v for k, v in self._last_debug.items() if k != "nachlauf"}
        execute = bool((self.data or {}).get("execute", False))
        return {
            "profile": self._profile,
            "apply_enabled": self.apply_enabled,
            "execute": execute,
            "ramp_active": self._ramp_active,
            "ramp_step": self._ramp_step,
            "ramp_total": self._ramp_total,
            "debounce": {
                "window_s": s.debounce_seconds,
                "max_wait_s": s.debounce_max_wait_s,
                "age_s": self._debounce_age(),
                "denon_immediate": s.denon_immediate,
                "pending": self._debounce_unsub is not None,
                "remaining_s": self._debounce_remaining(),
                # Der eine konsolidierte, noch nicht ausgeführte Plan (latest-wins,
                # KEINE Stale-FIFO) — Cockpit zeigt damit „was als Nächstes käme".
                "plan": self._pending_plan.as_dict() if self._pending_plan else None,
            },
            "reapply": {
                "pending": self._reapply_unsub is not None,
                "delay_s": self._reapply_delay_s,
                "remaining_s": self._reapply_remaining(),
                "reason": self._reapply_reason if self._reapply_unsub is not None else None,
            },
            "plan": plan,
            "log": list(self._log),
            "gates": {
                "apply_enabled": self.apply_enabled,
                "volume_apply_allowed": inp.volume_apply_allowed,
                "execute": execute,
                "stop_latch": inp.stop_latch,
            },
            "policy": {
                "action": inp.action,
                "homepods_should_pause": inp.homepods_should_pause,
                "homepods_resume_allowed": inp.homepods_resume_allowed,
                "homepods_target": inp.homepods_target,
                "denon_target": inp.denon_target,
                "subwoofer_allowed": inp.subwoofer_allowed,
                "quiet_mode": inp.quiet_mode,
            },
            "devices": {
                "homepods": {"configured": inp.homepods_configured, "state": inp.homepods_state, "volume": inp.homepods_volume},
                "denon": {"configured": inp.denon_configured, "state": inp.denon_state, "volume": inp.denon_volume, "power_on": inp.denon_power_on},
                "subwoofer": {"configured": inp.subwoofer_configured, "state": inp.subwoofer_state},
            },
            "nachlauf": {
                "active": self._nachlauf_state.pc_armed or self._nachlauf_state.tv_armed,
                "pc_armed": self._nachlauf_state.pc_armed,
                "tv_armed": self._nachlauf_state.tv_armed,
                "tv_paused": self._nachlauf_state.tv_paused,
                "pc_power_on": inp.pc_power_on,
                "tv_power_on": inp.tv_power_on,
                "bio_sleep": inp.bio_sleep,
                "denon_consumer_active": inp.denon_consumer_active,
                "tasks": sorted(self._nachlauf_tasks),
            },
            "tv_wol": {
                **self._tv_wol_state.as_dict(),
                "media_device": inp.media_device,
                "tv_player_state": inp.tv_player_state,
                "is_screen": inp.media_device in SCREEN_DEVICES,
                "screen_devices": list(SCREEN_DEVICES),
                "mac_configured": bool(self._opts.get(CONF_TV_WOL_MAC)),
            },
            "sleep_tv": {
                "armed": self._sleep_tv_state.armed,
                "running": self._sleep_tv_task is not None and not self._sleep_tv_task.done(),
                "bio_sleep": inp.bio_sleep,
                "tv_player_state": inp.tv_player_state,
                "delay_s": self._duration(CONF_SLEEP_TV_OFF_DELAY, DEFAULT_SLEEP_TV_OFF_DELAY),
                "warn_lead_s": self._duration(CONF_SLEEP_TV_WARN_LEAD, DEFAULT_SLEEP_TV_WARN_LEAD),
                "notify": str(self._opts.get(CONF_SLEEP_TV_NOTIFY, DEFAULT_SLEEP_TV_NOTIFY) or "") or None,
                "extend_bound": bool(self._entity_id(CONF_SLEEP_TV_EXTEND)),
                "evidence": (self.data or {}).get("sleep_tv_evidence", "inactive"),
                **self._sleep_tv_evidence_attrs(
                    (self.data or {}).get("sleep_tv_evidence", "inactive")
                ),
            },
            "wake": {
                "running": self._wake_task is not None and not self._wake_task.done(),
                "bio_state": self._state(CONF_BIO_STATE),   # primäre Quelle (core_state)
                "extra_triggers": self._entity_id(CONF_WAKE_TRIGGERS),
                "start_volume": s.wake_start_volume,
                "debounce_s": s.wake_debounce_seconds,
                "bio_sleep": inp.bio_sleep,
                "stop_latch_reset_reason": self._last_stop_latch_reset_reason,
                "stop_latch_reset_at": self._last_stop_latch_reset_at,
            },
            "playback_recovery": {
                "running": (
                    self._playback_recovery_task is not None
                    and not self._playback_recovery_task.done()
                ),
                "single_flight_owner": self._wake_start_owned,
                "health": self._playback_health,
                "reason": self._playback_health_reason,
                "stage": self._playback_recovery_stage,
                "attempts": self._playback_recovery_attempts,
                "soft_enabled": self._playback_recovery_enabled,
                "hard_enabled": bool(
                    self._opts.get(
                        CONF_PLAYBACK_HARD_RECOVERY,
                        DEFAULT_PLAYBACK_HARD_RECOVERY,
                    )
                ),
                "hard_configured": bool(
                    str(
                        self._opts.get(
                            CONF_MUSIC_ASSISTANT_APP_ID,
                            DEFAULT_MUSIC_ASSISTANT_APP_ID,
                        )
                        or ""
                    ).strip()
                ),
                "hard_cooldown_remaining_s": self._hard_recovery_remaining(),
            },
            "settings": {
                "ramp_steps": s.ramp_steps,
                "ramp_step_delay_s": s.ramp_step_delay_s,
                "tiny_delta": s.tiny_delta,
                "ducked_level": s.ducked_level,
                "debounce_seconds": s.debounce_seconds,
                "wake_start_volume": s.wake_start_volume,
                "wake_debounce_seconds": s.wake_debounce_seconds,
            },
            # Radio-Shortcuts fürs Cockpit (Defaults; Suche läuft via Action).
            "radio": {
                "defaults": logic.radio_defaults(),
                "autostart_enabled": self._radio_autostart_enabled,
                "ready": inp.radio_ready,
                "manual_playback": inp.manual_playback,
                "planned_station_playing": inp.planned_station_playing,
                "resume_pending": self._radio_resume_task is not None and not self._radio_resume_task.done(),
                "automatic_dispatch": self._radio_dispatch_status(),
            },
            # FLEET-44/98: private_time-Latch lebt jetzt nativ in media_state.
            "bindings": self.bindings(),
        }

    def debug(self) -> dict[str, Any]:
        return {
            **self._last_debug,
            "ramp_active": self._ramp_active,
            "debounce_pending": self._debounce_unsub is not None,
            "nachlauf": {
                "pc_armed": self._nachlauf_state.pc_armed,
                "tv_armed": self._nachlauf_state.tv_armed,
                "tv_paused": self._nachlauf_state.tv_paused,
                "tasks": sorted(self._nachlauf_tasks),
            },
            "bindings": self.bindings(),
        }

    # ----- execution (side effects) -----
    async def _svc(
        self, domain: str, service: str, data: dict[str, Any], blocking: bool = False
    ) -> None:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=blocking)
        except Exception as err:  # noqa: BLE001 — Geräte-Fehler dürfen Apply nicht crashen.
            _LOGGER.warning("media_apply: %s.%s %s failed: %s", domain, service, data, err)

    async def _execute(self, plan: logic.ApplyPlan) -> None:
        hp = self._entity_id(CONF_HOMEPODS_PLAYER)
        denon = self._entity_id(CONF_DENON_PLAYER)
        sub = self._entity_id(CONF_SUBWOOFER_SWITCH)

        if plan.quiet_override or plan.away_block:
            self._cancel_ramp()

        # ----- HomePods-Action -----
        if hp:
            if plan.homepods_action == ACTION_PAUSE:
                self._capture_resume_content(hp)
                await self._svc("media_player", "media_pause", {"entity_id": hp})
            elif plan.homepods_action == ACTION_RESUME:
                self._schedule_resume_reconciliation(source="tv_resume")
            elif plan.homepods_action == ACTION_START_RADIO:
                self._schedule_resume_reconciliation(source="policy")

        # ----- HomePods-Volume (Ramp oder direkt) — PRO POD (benni_media#16) -----
        vol_targets = self._homepods_volume_targets()
        idle_policy_start = (
            plan.homepods_action == ACTION_START_RADIO
            and self._state(CONF_HOMEPODS_PLAYER) not in PLAYER_PLAYING_VALUES
            and self._playback_recovery_source != "wake"
        )
        if plan.homepods_levels and vol_targets and not idle_policy_start:
            self._cancel_ramp()
            if plan.homepods_ramp:
                self._ramp_task = self.hass.async_create_task(
                    self._run_ramp(vol_targets, list(plan.homepods_levels), self.settings().ramp_step_delay_s)
                )
            else:
                await self._svc(
                    "media_player", "volume_set",
                    {"entity_id": vol_targets, "volume_level": plan.homepods_levels[-1]},
                )

        # ----- Denon-Volume (hart) -----
        if plan.denon_set is not None and denon:
            await self._svc(
                "media_player", "volume_set",
                {"entity_id": denon, "volume_level": plan.denon_set},
            )

        # ----- Denon-Aktion -----
        if plan.denon_action == ACTION_DENON_OFF and denon:
            await self._svc("media_player", "turn_off", {"entity_id": denon})

        # ----- Subwoofer-Plug -----
        if plan.subwoofer_set is not None and sub:
            await self._svc("switch", "turn_on" if plan.subwoofer_set else "turn_off", {"entity_id": sub})

    @callback
    def _cancel_ramp(self) -> None:
        if self._ramp_task is not None and not self._ramp_task.done():
            self._ramp_task.cancel()
        self._ramp_task = None
        self._set_ramp_active(False)

    @callback
    def _set_ramp_active(self, active: bool) -> None:
        if self._ramp_active == active:
            return
        self._ramp_active = active
        if not active:
            self._ramp_step = 0
            self._ramp_total = 0
        if self.data is not None:
            self.async_set_updated_data({**self.data, "ramp_active": active})

    async def _run_ramp(
        self, entity_ids: list[str] | str, levels: list[float], delay: float
    ) -> None:
        """HomePods-Volume-Ramp: Schritt für Schritt mit Delay, abbrechbar.

        benni_media#16 — `entity_ids` ist die Pod-Liste (kein Gruppen-Call). Ein
        einzelner `volume_set` mit Entity-Liste setzt alle Pods gemeinsam; ein
        String bleibt als Fallback (Gruppe) unterstützt."""
        self._ramp_total = len(levels)
        self._set_ramp_active(True)
        try:
            for i, lv in enumerate(levels):
                self._ramp_step = i + 1
                await self.hass.services.async_call(
                    "media_player", "volume_set",
                    {"entity_id": entity_ids, "volume_level": lv}, blocking=True,
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("media_apply: ramp on %s failed: %s", entity_ids, err)
        finally:
            self._set_ramp_active(False)

    # ----- Radio-Autostart (FLEET-79) -----
    def _manual_off_edge(self) -> bool:
        """manual_playback True→False (Trigger B). Nur EINMAL pro Tick (mutiert)."""
        cur = self._tri_bool(CONF_MANUAL_PLAYBACK)
        prev = self._last_manual_playback
        self._last_manual_playback = cur
        return prev is True and cur is False

    @callback
    def _schedule_radio_autostart(self) -> None:
        if self._playback_recovery_task is not None and not self._playback_recovery_task.done():
            return
        self._cancel_playback_recovery("new_wake")
        inp = self._build_inputs()
        reason = self._playback_start_block_reason(
            inp, source="wake", admission=True
        )
        if reason is not None:
            self._set_playback_recovery("cancelled", health="inactive", reason=reason)
            return
        self._wake_start_owned = True
        self._playback_episode_admitted = True
        self._playback_episode_seen_playing = False
        self._playback_health = "settling"
        self._playback_health_reason = None
        self._playback_recovery_stage = "initial_start"
        self._playback_recovery_source = "wake"
        self._playback_action_log = None
        self._playback_recovery_attempts = 0
        self._playback_recovery_started_at = self.hass.loop.time()
        self._playback_recovery_task = self.hass.async_create_task(
            self._run_radio_autostart()
        )

    @callback
    def _schedule_resume_reconciliation(self, *, source: str) -> None:
        """Claim the existing single flight before any resume/replace side effect."""
        inp = self._build_inputs()
        if self._wake_start_owned or self._resume_episode_finished:
            return
        if source == "tv_resume" and self._resume_content_changed_since_pause():
            self._resume_episode_finished = True
            self._set_playback_recovery(
                "cancelled", health="inactive", reason="resume_content_changed"
            )
            return
        start_inp = self._resume_start_inputs(inp, source)
        reason = self._playback_start_block_reason(
            start_inp, source=source, admission=True
        )
        if reason is not None:
            self._set_playback_recovery("cancelled", health="inactive", reason=reason)
            return
        self._cancel_radio_resume()
        self._cancel_stuck_mute_recovery()
        self._wake_start_owned = True  # compatibility name for the shared owner
        self._playback_episode_admitted = True
        self._playback_episode_seen_playing = (
            self._state(CONF_HOMEPODS_PLAYER) in PLAYER_PLAYING_VALUES
        )
        self._resume_episode_expected_content_id = self._resume_content_id
        self._resume_episode_seen_playing = False
        self._resume_episode_dispatched = False
        self._playback_recovery_source = source
        self._playback_recovery_started_at = self.hass.loop.time()
        self._playback_recovery_attempts = 0
        self._playback_action_log = next((row for row in self._log if row["action"] in (ACTION_RESUME, ACTION_START_RADIO)), None)
        self._set_playback_recovery("settling", health="settling")
        self._playback_recovery_task = self.hass.async_create_task(self._run_radio_autostart(source=source))

    @callback
    def _cancel_playback_recovery(self, reason: str) -> None:
        task = self._playback_recovery_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._playback_recovery_task = None
        self._wake_start_owned = False
        self._playback_episode_admitted = False
        self._playback_episode_seen_playing = False
        if self._playback_recovery_stage not in ("idle", "healthy", "failed"):
            self._playback_recovery_stage = "cancelled"
            self._playback_health = "inactive"
            self._playback_health_reason = reason
            if self._playback_action_log is not None:
                self._playback_action_log.update(executed=False, playback_health="inactive", recovery_stage="cancelled", reason=reason)

    def _set_playback_recovery(
        self, stage: str, *, health: str | None = None, reason: str | None = None
    ) -> None:
        self._playback_recovery_stage = stage
        if health is not None:
            self._playback_health = health
        self._playback_health_reason = reason
        if self._playback_action_log is not None:
            self._playback_action_log.update(executed=health == "healthy", playback_health=self._playback_health, recovery_stage=stage, reason=reason)
        if self.data is not None:
            self.async_set_updated_data({
                **self.data,
                "playback_health": self._playback_health,
                "playback_health_attrs": self._playback_health_attrs(),
                "playback_recovery_stage": self._playback_recovery_stage,
            })

    def _playback_health_attrs(self) -> dict[str, Any]:
        return {
            "reason": self._playback_health_reason,
            "source": self._playback_recovery_source,
            "attempts": self._playback_recovery_attempts,
            "wake_start_owned": self._wake_start_owned,
            "single_flight_owner": self._wake_start_owned,
            "backstop_interval_seconds": DEFAULT_STUCK_MUTE_BACKSTOP_INTERVAL,
            "unmute_cooldown_remaining_seconds": round(
                max(0.0, self._stuck_mute_retry_not_before - self.hass.loop.time()),
                1,
            ),
        }

    def _managed_playback_episode(self, inp: logic.Inputs) -> bool:
        return self._wake_start_owned or inp.planned_station_playing is True

    def _episode_group_has_played(self) -> bool:
        if self._playback_episode_seen_playing:
            return True
        if self._state(CONF_HOMEPODS_PLAYER) in PLAYER_PLAYING_VALUES:
            self._playback_episode_seen_playing = True
        return self._playback_episode_seen_playing

    def _playback_start_block_reason(
        self,
        inp: logic.Inputs,
        *,
        source: str,
        admission: bool,
    ) -> str | None:
        """Apply the one-time episode admission rule before a start side effect."""
        seen_playing = self._episode_group_has_played()
        owner = (inp.audio_owner or "").strip().lower()
        gate_inp = self._resume_start_inputs(inp, source)
        if owner in ("", "none") and not seen_playing:
            gate_inp = replace(gate_inp, audio_owner="homepods")
        if not admission or source == "policy":
            gate_inp = replace(gate_inp, homepods_resume_allowed=True)
        if admission and source == "wake":
            gate_inp = replace(gate_inp, stop_latch=False)
        reason = logic.playback_start_block_reason(
            gate_inp, require_positive_target=False
        )
        if reason is not None:
            return reason
        if inp.homepods_should_pause:
            return "policy_pause"
        if inp.bio_state in BIO_SLEEP_CONTEXT_VALUES:
            return "sleep_context"
        if not inp.volume_apply_allowed:
            return "volume_apply_not_allowed"
        if owner in ("unknown", "unavailable"):
            return "audio_owner_unproven"
        if seen_playing and (
            inp.homepods_target is None or inp.homepods_target <= 0.0
        ):
            return "non_positive_target"
        return None

    @staticmethod
    def _media_content_value(state_obj: Any, attribute: str) -> str | None:
        value = state_obj.attributes.get(attribute) if state_obj is not None else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _capture_resume_content(self, player: str) -> None:
        state_obj = self.hass.states.get(player)
        if state_obj is not None:
            self._resume_content_id = self._media_content_value(
                state_obj, "media_content_id"
            )
            self._resume_content_type = self._media_content_value(
                state_obj, "media_content_type"
            )
            self._resume_content_player = player
        else:
            self._resume_content_id = None
            self._resume_content_type = None
            self._resume_content_player = None
        self._resume_episode_expected_content_id = None
        self._resume_episode_seen_playing = False
        self._resume_episode_dispatched = False
        self._resume_episode_finished = False

    def _resume_user_override_reason(self) -> str | None:
        if self._playback_recovery_source != "tv_resume" or not self._wake_start_owned:
            return None
        player = self._resume_content_player or self._entity_id(CONF_HOMEPODS_PLAYER)
        state_obj = self.hass.states.get(player) if player else None
        if state_obj is None:
            return None
        current = self._media_content_value(state_obj, "media_content_id")
        expected = self._resume_episode_expected_content_id
        if expected is not None and current is not None and current != expected:
            return "resume_content_changed"
        if state_obj.state in PLAYER_PLAYING_VALUES:
            self._resume_episode_seen_playing = True
            return None
        if self._resume_episode_seen_playing:
            return "resume_user_stopped"
        return None

    def _resume_content_changed_since_pause(self) -> bool:
        if self._resume_content_id is None or self._resume_content_player is None:
            return False
        state_obj = self.hass.states.get(self._resume_content_player)
        current = self._media_content_value(state_obj, "media_content_id")
        return current is not None and current != self._resume_content_id

    def _playback_repair_block_reason(
        self,
        inp: logic.Inputs,
        *,
        managed_episode: bool,
        require_positive_target: bool = True,
    ) -> str | None:
        episode_active = managed_episode and self._wake_start_owned
        if not episode_active:
            return logic.playback_repair_block_reason(
                inp,
                managed_episode=managed_episode,
                require_positive_target=require_positive_target,
            )
        seen_playing = self._episode_group_has_played()
        owner = (inp.audio_owner or "").strip().lower()
        if owner in ("unknown", "unavailable"):
            return "audio_owner_unproven"
        if owner in ("", "none") and not seen_playing:
            inp = replace(inp, audio_owner="homepods")
        override = self._resume_user_override_reason()
        if override is not None:
            return override
        if inp.manual_playback is True and self._playback_recovery_source == "tv_resume":
            player = self._resume_content_player or self._entity_id(CONF_HOMEPODS_PLAYER)
            state_obj = self.hass.states.get(player) if player else None
            if state_obj is not None:
                current = self._media_content_value(state_obj, "media_content_id")
                expected = self._resume_episode_expected_content_id
                owned_pause = (
                    not self._resume_episode_seen_playing
                    and not self._resume_episode_dispatched
                    and self._resume_content_player is not None
                )
                if owned_pause or (
                    state_obj.state in PLAYER_PLAYING_VALUES
                    and (expected is None or current is None or current == expected)
                ):
                    inp = replace(inp, manual_playback=False)
        return logic.playback_repair_block_reason(
            inp,
            managed_episode=managed_episode,
            require_positive_target=seen_playing,
        )

    def _resume_start_inputs(self, inp: logic.Inputs, source: str) -> logic.Inputs:
        if (
            source == "tv_resume"
            and inp.manual_playback is True
            and self._resume_content_player is not None
            and not self._resume_content_changed_since_pause()
        ):
            return replace(inp, manual_playback=False)
        return inp

    def _resume_content_is_radio(self) -> bool:
        content_type = (self._resume_content_type or "").lower()
        return content_type in {"radio", "channel"} or bool(
            self._resume_content_id
            and self._resume_content_id.startswith("radiobrowser://")
        )

    async def _dispatch_resume_media_play(self) -> bool:
        reason = self._repair_block_reason(require_positive_target=False)
        player = self._resume_content_player or self._entity_id(CONF_HOMEPODS_PLAYER)
        if reason is not None or not player:
            return False
        try:
            await self.hass.services.async_call(
                "media_player", "media_play", {"entity_id": player}, blocking=True
            )
            self._resume_episode_dispatched = True
            if self._playback_action_log is not None:
                self._playback_action_log["dispatched"] = True
            return True
        except Exception as err:  # noqa: BLE001
            self._playback_health_reason = (
                f"resume_dispatch_failed:{type(err).__name__}"
            )
            return False

    def _repair_block_reason(
        self, *, require_positive_target: bool = True
    ) -> str | None:
        inp = self._build_inputs()
        return self._playback_repair_block_reason(
            inp,
            managed_episode=self._managed_playback_episode(inp),
            require_positive_target=require_positive_target,
        )

    async def _recovery_sleep(
        self, delay: float, *, require_positive_target: bool = True
    ) -> bool:
        await asyncio.sleep(max(0.0, delay))
        reason = self._repair_block_reason(
            require_positive_target=require_positive_target
        )
        if reason is not None:
            self._set_playback_recovery("cancelled", health="inactive", reason=reason)
            return False
        return True

    def _playback_member_snapshot(
        self,
    ) -> tuple[str | None, list[str], list[str | None], list[bool | None]]:
        group = self._entity_id(CONF_HOMEPODS_PLAYER)
        group_obj = self.hass.states.get(group) if group else None
        pods = self._homepods_volume_targets()
        pod_states: list[str | None] = []
        pod_muted: list[bool | None] = []
        for entity_id in pods:
            state = self.hass.states.get(entity_id)
            pod_state = (
                state.state
                if state is not None and state.state not in ("unknown", "unavailable")
                else None
            )
            pod_states.append(pod_state)
            if pod_state is None:
                pod_muted.append(None)
            elif self._supports_volume_mute(state):
                pod_muted.append(state.attributes["is_volume_muted"])
            else:
                pod_muted.append(False)
        group_state = (
                group_obj.state
                if group_obj is not None
                and group_obj.state not in ("unknown", "unavailable")
                else None
            )
        return group_state, pods, pod_states, pod_muted

    @staticmethod
    def _supports_volume_mute(state: Any) -> bool:
        """Return whether a reachable player exposes reliable mute control."""
        if state is None:
            return False
        supported = state.attributes.get("supported_features")
        muted = state.attributes.get("is_volume_muted")
        return (
            isinstance(supported, int)
            and bool(supported & int(MediaPlayerEntityFeature.VOLUME_MUTE))
            and isinstance(muted, bool)
        )

    def _mute_capable_homepods(
        self, entity_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        return tuple(
            entity_id
            for entity_id in entity_ids
            if self._supports_volume_mute(self.hass.states.get(entity_id))
        )

    def _playback_health_snapshot(self) -> logic.PlaybackHealth:
        group_state, _pods, pod_states, pod_muted = self._playback_member_snapshot()
        return logic.playback_health(
            group_state=group_state,
            pod_states=pod_states,
            pod_muted=pod_muted,
            target=self._float(CONF_VOL_TARGET_HOMEPODS),
        )

    def _stuck_mute_targets(self) -> tuple[str, ...]:
        group_state, pods, pod_states, pod_muted = self._playback_member_snapshot()
        return logic.stuck_mute_targets(
            group_state=group_state,
            entity_ids=pods,
            pod_states=pod_states,
            pod_muted=pod_muted,
        )

    def _unconfirmed_unmute_targets(
        self, targets: tuple[str, ...]
    ) -> tuple[str, ...]:
        _group_state, pods, _pod_states, pod_muted = self._playback_member_snapshot()
        muted_by_entity = dict(zip(pods, pod_muted))
        return tuple(
            entity_id
            for entity_id in targets
            if muted_by_entity.get(entity_id) is not False
        )

    async def _stable_playback_health(self) -> logic.PlaybackHealth:
        """Require consecutive healthy samples so a 5s AirPlay flap is visible."""
        health = self._playback_health_snapshot()
        for sample in range(DEFAULT_PLAYBACK_HEALTH_SAMPLES):
            if health.state != "healthy":
                return health
            if sample == DEFAULT_PLAYBACK_HEALTH_SAMPLES - 1:
                break
            if not await self._recovery_sleep(
                DEFAULT_PLAYBACK_HEALTH_SAMPLE_INTERVAL
            ):
                return logic.PlaybackHealth(
                    "cancelled", self._playback_health_reason
                )
            health = self._playback_health_snapshot()
        return health

    async def _wait_for_playing_homepods(self, timeout: float = 15.0) -> bool:
        deadline = self.hass.loop.time() + max(0.0, timeout)
        while True:
            group_state, _pods, pod_states, _muted = self._playback_member_snapshot()
            if (
                group_state in PLAYER_PLAYING_VALUES
                and pod_states
                and all(state in PLAYER_PLAYING_VALUES for state in pod_states)
            ):
                return True
            remaining = deadline - self.hass.loop.time()
            if remaining <= 0.0:
                return False
            if not await self._recovery_sleep(
                min(1.0, remaining), require_positive_target=False
            ):
                return False

    async def _unmute_homepods(
        self,
        *,
        source: str,
        force_all: bool = False,
        update_diagnostics: bool = False,
    ) -> logic.UnmuteResult:
        reason = self._repair_block_reason()
        if reason is not None:
            return logic.UnmuteResult("cancelled", 0, reason=reason)
        pods = tuple(self._homepods_volume_targets())
        requested = pods if force_all else self._stuck_mute_targets()
        targets = self._mute_capable_homepods(requested)
        if not targets:
            return logic.UnmuteResult("not_needed", 0)

        async def _call(entity_ids: tuple[str, ...]) -> None:
            self._last_unmute_attempt_at = self.hass.loop.time()
            await self.hass.services.async_call(
                "media_player",
                "volume_mute",
                {"entity_id": list(entity_ids), "is_volume_muted": False},
                blocking=True,
            )

        def _block_reason() -> str | None:
            current_reason = self._repair_block_reason()
            if current_reason is not None:
                return current_reason
            group_state, _pods, pod_states, _muted = self._playback_member_snapshot()
            if group_state not in PLAYER_PLAYING_VALUES or not pod_states:
                return "playback_not_playing"
            if not all(state in PLAYER_PLAYING_VALUES for state in pod_states):
                return "playback_not_playing"
            return None

        result = await logic.bounded_unmute(
            targets,
            unmute=_call,
            remaining_muted=lambda: self._unconfirmed_unmute_targets(targets),
            wait=asyncio.sleep,
            block_reason=_block_reason,
            max_attempts=DEFAULT_STUCK_MUTE_RETRY_ATTEMPTS,
            retry_delay=DEFAULT_STUCK_MUTE_RETRY_DELAY,
        )
        self._playback_recovery_attempts += result.attempts
        if update_diagnostics:
            self._playback_recovery_source = source
            if result.state in ("recovered", "not_needed"):
                health = self._playback_health_snapshot()
                self._set_playback_recovery(
                    "unmute_recovered",
                    health="healthy" if health.state == "healthy" else health.state,
                    reason=health.reason,
                )
            elif result.state == "cancelled":
                self._set_playback_recovery(
                    "cancelled", health="inactive", reason=result.reason
                )
            else:
                self._set_playback_recovery(
                    "unmute_failed", health="unhealthy", reason=result.reason
                )
                _LOGGER.warning(
                    "media_apply: HomePods unmute failed after %s attempts (%s)",
                    result.attempts,
                    result.reason,
                )
        return result

    @callback
    def _schedule_stuck_mute_recovery(self, *, source: str) -> None:
        if self._playback_recovery_task is not None and not self._playback_recovery_task.done():
            return
        reason = self._repair_block_reason()
        if reason is not None:
            self._cancel_stuck_mute_recovery()
            return
        if not self._stuck_mute_targets():
            return
        if self._stuck_mute_task is not None and not self._stuck_mute_task.done():
            return
        now = self.hass.loop.time()
        if now < self._stuck_mute_retry_not_before:
            return
        if (
            self._last_unmute_attempt_at is not None
            and now - self._last_unmute_attempt_at < DEFAULT_STUCK_MUTE_EVENT_COOLDOWN
        ):
            return
        self._stuck_mute_task = self.hass.async_create_task(
            self._run_stuck_mute_recovery(source)
        )

    @callback
    def _cancel_stuck_mute_recovery(self) -> None:
        task = self._stuck_mute_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._stuck_mute_task = None

    async def _run_stuck_mute_recovery(self, source: str) -> None:
        self._playback_recovery_source = source
        self._set_playback_recovery(
            "unmute_recovery",
            health="unhealthy",
            reason="stuck_mute_detected",
        )
        try:
            result = await self._unmute_homepods(
                source=source, force_all=False, update_diagnostics=True
            )
            if result.state == "failed":
                self._stuck_mute_retry_not_before = (
                    self.hass.loop.time() + DEFAULT_STUCK_MUTE_BACKSTOP_INTERVAL
                )
                if self.data is not None:
                    self.async_set_updated_data(
                        {
                            **self.data,
                            "playback_health_attrs": self._playback_health_attrs(),
                        }
                    )
        except asyncio.CancelledError:
            raise
        finally:
            if self._stuck_mute_task is asyncio.current_task():
                self._stuck_mute_task = None

    async def _run_radio_autostart(self, *, source: str = "wake") -> None:
        """Single-flight Wake-Start with soft and optional hard recovery (#41)."""
        self._playback_recovery_source = source
        is_resume = source != "wake"
        # Race-Fix: Auf derselben Wake-Flanke setzt _run_wake parallel den
        # Volume-Floor (0.10, blockierend). Kurzer Vorlauf, damit der Floor anliegt,
        # bevor wir Ton ausgeben — sonst Burst bei alter Lautstärke (FLEET-42).
        lead = self._duration(CONF_WAKE_PLAY_LEAD, DEFAULT_WAKE_PLAY_LEAD)
        if lead > 0 and not is_resume:
            try:
                await asyncio.sleep(lead)
            except asyncio.CancelledError:
                raise
        try:
            inp = self._build_inputs()
            start_inp = self._resume_start_inputs(inp, source)
            if self._playback_episode_admitted and self._episode_group_has_played():
                reason = self._playback_repair_block_reason(
                    start_inp,
                    managed_episode=True,
                    require_positive_target=True,
                )
            else:
                reason = self._playback_start_block_reason(
                    start_inp,
                    source=source,
                    admission=not self._playback_episode_admitted,
                )
            if reason is not None:
                self._set_playback_recovery(
                    "cancelled", health="inactive", reason=reason
                )
                return
            self._playback_episode_admitted = True
            uri = logic.resolve_radio_uri(inp.radio_station)
            fallback_dispatched = False
            if source == "tv_resume":
                if self._state(CONF_HOMEPODS_PLAYER) not in PLAYER_PLAYING_VALUES:
                    if self._resume_content_id is not None:
                        await self._dispatch_resume_media_play()
                    else:
                        self._resume_episode_expected_content_id = uri
                        fallback_dispatched = await self._dispatch_automatic_radio(
                            uri,
                            source="tv_resume_fallback",
                            replace_existing=True,
                            bypass_circuit=True,
                            require_positive_target=False,
                        )
                        self._resume_episode_dispatched = fallback_dispatched
                        if not fallback_dispatched:
                            self._set_playback_recovery(
                                "failed",
                                health="unhealthy",
                                reason=self._radio_dispatch_state.last_error
                                or "fallback_dispatch_blocked",
                            )
                            return
            elif logic.should_autostart_radio(inp):
                await self._dispatch_automatic_radio(uri, source="wake_autostart" if not is_resume else source)

            # play_media startet selbst. Nach kurzem Settle explizit entstummen;
            # der alte zusätzliche media_play-Impuls war Teil des Lock-Races.
            self._set_playback_recovery("settling", health="settling")
            if not await self._recovery_sleep(2.0, require_positive_target=False):
                return
            if await self._wait_for_playing_homepods():
                await self._unmute_homepods(
                    source=f"{source}_initial", force_all=True
                )

            if not self._playback_recovery_enabled:
                if is_resume:
                    health = await self._stable_playback_health()
                    if health.state == "cancelled":
                        return
                    self._set_playback_recovery("healthy" if health.state == "healthy" else "failed", health=health.state, reason=health.reason)
                else:
                    self._set_playback_recovery("complete", health="unmonitored")
                return

            settle = self._duration(
                CONF_PLAYBACK_RECOVERY_SETTLE, DEFAULT_PLAYBACK_RECOVERY_SETTLE
            )
            elapsed = self.hass.loop.time() - (self._playback_recovery_started_at or 0.0)
            if not await self._recovery_sleep(
                max(0.0, settle - elapsed),
                require_positive_target=False,
            ):
                return
            health = await self._stable_playback_health()
            if health.state == "cancelled":
                return
            if health.state == "healthy":
                self._set_playback_recovery("healthy", health="healthy")
                return

            # Soft-Recovery: genau ein kompletter Stream-Replace, anschließend
            # explizites Unmute. Kein MA-App-Neustart im ersten Fehlerfenster.
            self._playback_recovery_attempts += 1
            self._set_playback_recovery(
                "soft_recovery", health=health.state, reason=health.reason
            )
            if source == "tv_resume" and self._resume_content_id is None:
                dispatched = fallback_dispatched
            elif source == "tv_resume" and self._resume_content_is_radio():
                dispatched = await self._dispatch_automatic_radio(
                    self._resume_content_id,
                    source="tv_resume_soft_recovery",
                    replace_existing=True,
                    bypass_circuit=True,
                    require_positive_target=False,
                )
            elif source == "tv_resume":
                dispatched = await self._dispatch_resume_media_play()
            else:
                dispatched = await self._dispatch_automatic_radio(
                    uri,
                    source=f"{source}_soft_recovery",
                    replace_existing=True,
                    bypass_circuit=True,
                )
            if source == "tv_resume" and self._resume_content_id is None:
                self._set_playback_recovery(
                    "failed", health=health.state, reason=health.reason
                )
                return
            if is_resume and not dispatched:
                self._set_playback_recovery("failed", health="unhealthy", reason=self._radio_dispatch_state.last_error or "recovery_dispatch_blocked")
                return
            if not await self._recovery_sleep(2.0, require_positive_target=False):
                return
            if await self._wait_for_playing_homepods():
                await self._unmute_homepods(
                    source=f"{source}_soft_recovery", force_all=True
                )
            if not await self._recovery_sleep(
                DEFAULT_PLAYBACK_RECOVERY_RECHECK, require_positive_target=False
            ):
                return
            health = await self._stable_playback_health()
            if health.state == "cancelled":
                return
            if health.state == "healthy":
                self._set_playback_recovery("healthy", health="healthy")
                return

            if is_resume:
                # Normal resume has one bounded replace, no periodic recovery
                # and no implicit extension of the Wake-only app restart gate.
                self._set_playback_recovery("failed", health=health.state, reason=health.reason)
                return

            hard_after = self._duration(
                CONF_PLAYBACK_RECOVERY_HARD_AFTER,
                DEFAULT_PLAYBACK_RECOVERY_HARD_AFTER,
            )
            elapsed = self.hass.loop.time() - (self._playback_recovery_started_at or 0.0)
            if not await self._recovery_sleep(max(0.0, hard_after - elapsed)):
                return
            health = await self._stable_playback_health()
            if health.state == "cancelled":
                return
            if health.state == "healthy":
                self._set_playback_recovery("healthy", health="healthy")
                return
            if not await self._restart_music_assistant(uri, health):
                self._set_playback_recovery(
                    "failed",
                    health=health.state,
                    reason=self._playback_health_reason or health.reason,
                )
                return

            final_settle = self._duration(
                CONF_PLAYBACK_RECOVERY_SETTLE, DEFAULT_PLAYBACK_RECOVERY_SETTLE
            )
            if not await self._recovery_sleep(final_settle):
                return
            health = await self._stable_playback_health()
            if health.state == "cancelled":
                return
            self._set_playback_recovery(
                "healthy" if health.state == "healthy" else "failed",
                health=health.state,
                reason=health.reason,
            )
        except asyncio.CancelledError:
            raise
        finally:
            if self._playback_recovery_task is asyncio.current_task():
                self._playback_recovery_task = None
                self._wake_start_owned = False
                if is_resume:
                    self._resume_episode_finished = True

    async def _restart_music_assistant(
        self, uri: str | None, health: logic.PlaybackHealth
    ) -> bool:
        enabled = bool(
            self._opts.get(CONF_PLAYBACK_HARD_RECOVERY, DEFAULT_PLAYBACK_HARD_RECOVERY)
        )
        app_id = str(
            self._opts.get(CONF_MUSIC_ASSISTANT_APP_ID, DEFAULT_MUSIC_ASSISTANT_APP_ID)
            or ""
        ).strip()
        now = self.hass.loop.time()
        cooldown = self._duration(
            CONF_MUSIC_ASSISTANT_RESTART_COOLDOWN,
            DEFAULT_MUSIC_ASSISTANT_RESTART_COOLDOWN,
        )
        if not enabled or not app_id:
            return False
        if (
            self._last_hard_recovery_at is not None
            and now - self._last_hard_recovery_at < cooldown
        ):
            self._playback_health_reason = "hard_recovery_cooldown"
            return False
        if self._repair_block_reason() is not None:
            return False
        self._last_hard_recovery_at = now
        self._playback_recovery_attempts += 1
        self._set_playback_recovery(
            "hard_recovery", health=health.state, reason=health.reason
        )
        try:
            await self.hass.services.async_call(
                "hassio", "addon_restart", {"addon": app_id}, blocking=True
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self._playback_health_reason = f"ma_restart_failed:{err}"
            _LOGGER.warning("media_apply: Music Assistant restart failed: %s", err)
            return False
        wait = self._duration(
            CONF_MUSIC_ASSISTANT_RESTART_WAIT, DEFAULT_MUSIC_ASSISTANT_RESTART_WAIT
        )
        if not await self._recovery_sleep(wait):
            return False
        dispatched = await self._dispatch_automatic_radio(
            uri,
            source="wake_hard_recovery",
            replace_existing=True,
            bypass_circuit=True,
        )
        if not dispatched or not await self._recovery_sleep(2.0):
            return False
        if await self._wait_for_playing_homepods():
            await self._unmute_homepods(
                source="wake_hard_recovery", force_all=True
            )
        return True

    @callback
    def _schedule_radio_resume(self) -> None:
        self._cancel_radio_resume()
        self._radio_resume_task = self.hass.async_create_task(self._run_radio_resume())

    @callback
    def _cancel_radio_resume(self) -> None:
        if self._radio_resume_task is not None and not self._radio_resume_task.done():
            self._radio_resume_task.cancel()
        self._radio_resume_task = None

    @callback
    def _radio_dispatch_admit(self, source: str) -> bool:
        allowed, state, reason = logic.radio_dispatch_admit(
            self._radio_dispatch_state,
            self.hass.loop.time(),
            source=source,
        )
        if not allowed:
            _LOGGER.debug(
                "media_apply: automatic radio dispatch suppressed (%s), %.2fs remaining",
                source,
                logic.radio_dispatch_remaining(state, self.hass.loop.time()),
            )
            return False
        self._radio_dispatch_state = state
        self.async_update_listeners()
        return True

    @callback
    def _radio_dispatch_result(self, *, success: bool, error: Exception | None = None) -> None:
        self._radio_dispatch_state = logic.radio_dispatch_result(
            self._radio_dispatch_state,
            self.hass.loop.time(),
            success=success,
            error=str(error) if error else None,
        )
        self.async_update_listeners()

    def _radio_dispatch_status(self) -> dict[str, Any]:
        state = self._radio_dispatch_state
        return {
            "cooldown_s": logic.radio_dispatch_remaining(state, self.hass.loop.time()),
            "failure_count": state.consecutive_failures,
            "last_source": state.last_source,
            "last_error": state.last_error,
            "payload": self._radio_dispatch_payload,
        }

    async def _dispatch_automatic_radio(
        self,
        media_id: str | None,
        *,
        source: str,
        replace_existing: bool = False,
        bypass_circuit: bool = False,
        require_positive_target: bool = True,
    ) -> bool:
        """Dispatch one automatic radio start through the shared circuit breaker."""
        hp = self._entity_id(CONF_HOMEPODS_PLAYER)
        self._radio_dispatch_payload = {
            "target_bound": isinstance(hp, str) and hp.startswith("media_player."),
            "media_id_present": bool(media_id),
            "media_id_type": type(media_id).__name__,
            "media_scheme": next((scheme for scheme in ("http", "https", "radiobrowser", "library") if isinstance(media_id, str) and media_id.startswith(scheme + "://")), "other" if media_id else None),
            "media_type": RADIO_MEDIA_TYPE,
            "enqueue": RADIO_ENQUEUE,
            "source": source,
        }
        # Owned starts cannot delegate to a script that may add another play.
        invalid = None
        if not isinstance(hp, str) or not hp.startswith("media_player.") or self.hass.states.get(hp) is None:
            invalid = "target_player_missing_or_invalid"
        elif self._wake_start_owned and not media_id:
            invalid = "media_id_missing"
        elif media_id is not None and (not isinstance(media_id, str) or "://" not in media_id or not media_id.split("://", 1)[1] or any(char.isspace() for char in media_id)):
            invalid = "media_id_invalid"
        if invalid:
            self._radio_dispatch_result(success=False, error=ValueError(invalid))
            return False
        inp = self._build_inputs()
        episode_start_block = (
            self._playback_start_block_reason(
                inp,
                source=self._playback_recovery_source or source,
                admission=False,
            )
            if self._wake_start_owned and not replace_existing
            else None
        )
        recovery_block = (
            self._playback_repair_block_reason(
                inp,
                managed_episode=self._managed_playback_episode(inp),
                require_positive_target=require_positive_target,
            )
            if replace_existing
            else None
        )
        if (
            episode_start_block is not None
            or recovery_block is not None
            or inp.stop_latch
            or inp.radio_ready is False
            or (inp.manual_playback is True and not replace_existing)
            or logic.media_block_reason(inp) is not None
            or (
                not replace_existing
                and self._state(CONF_HOMEPODS_PLAYER) in PLAYER_PLAYING_VALUES
            )
        ):
            return False
        if not bypass_circuit and not self._radio_dispatch_admit(source):
            return False
        try:
            if media_id:
                await self.hass.services.async_call(
                    "music_assistant",
                    "play_media",
                    {
                        "entity_id": hp,
                        "media_id": media_id,
                        "media_type": RADIO_MEDIA_TYPE,
                        "enqueue": RADIO_ENQUEUE,
                    },
                    blocking=True,
                )
            else:
                radio = self._opts.get(CONF_RADIO_START_SCRIPT, DEFAULT_RADIO_START_SCRIPT)
                await self.hass.services.async_call(
                    "script", "turn_on", {"entity_id": radio}, blocking=True
                )
            self._radio_dispatch_result(success=True)
            if self._playback_action_log is not None:
                self._playback_action_log["dispatched"] = True
            _LOGGER.info(
                "media_apply: automatic radio dispatch (%s) → %s",
                source,
                self._radio_dispatch_payload["media_scheme"] if media_id else "script",
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — provider failure is circuit-breaker input.
            safe_error = RuntimeError(f"provider_dispatch_failed:{type(err).__name__}")
            self._radio_dispatch_result(success=False, error=safe_error)
            _LOGGER.warning("media_apply: automatic radio dispatch (%s) failed: %s", source, safe_error)
            return False

    async def _run_radio_resume(self) -> None:
        """Trigger B: nach Delay die geplante Station fortsetzen — re-prüft die
        Bedingungen (Latch off, ready, kein manual, nicht schon playing)."""
        delay = self._duration(CONF_RADIO_RESUME_DELAY, DEFAULT_RADIO_RESUME_DELAY)
        try:
            await asyncio.sleep(max(0.0, delay))
        except asyncio.CancelledError:
            raise
        self._radio_resume_task = None
        if not (self.apply_enabled and self._radio_autostart_enabled):
            return
        inp = self._build_inputs()
        latch_on = _bool(self._state(CONF_STOP_LATCH))
        if latch_on or not logic.should_autostart_radio(inp) or inp.action == ACTION_PAUSE:
            return
        self._schedule_resume_reconciliation(source="resume")

    def _tv_wol_block_reason(self, episode_id: int) -> str | None:
        """Recheck this task's permission immediately before each side effect."""
        state = self._tv_wol_state
        if state.episode_id != episode_id or not state.episode_active:
            return "r12:episode_ended"
        if state.last_consume_reason != "r12:wake_requested":
            return state.last_consume_reason or "r12:episode_consumed"
        if not self.apply_enabled:
            return "r12:shadow"
        inp = self._build_inputs()
        if logic.tv_screen_intent(inp) is not True:
            return "r12:screen_intent_changed"
        if logic._tv_is_off(inp) is not True:
            return "r12:tv_not_confirmed_off"
        return None

    async def _execute_tv_wol(self, episode_id: int) -> None:
        """R12: TV einschalten. `media_player.turn_on` löst das webOS-„Leuchtfeuer"
        aus (bleibt 24/7); ist zusätzlich eine MAC konfiguriert, sendet media_apply
        das Magic-Packet selbst (variabel pflegbar)."""
        if episode_id != self._tv_wol_state.episode_id:
            return  # An old task must not consume a newer episode's permission.
        reason = self._tv_wol_block_reason(episode_id)
        if reason:
            self._tv_wol_state.consume(reason)
            self._tv_wol_state.suppressed_reason = reason
            return
        if not self._tv_wol_state.pending:
            return
        self._tv_wol_state.pending = False  # claim before the first await
        tv = self._entity_id(CONF_TV_PLAYER)
        if tv:
            await self._svc("media_player", "turn_on", {"entity_id": tv})
        mac = str(self._opts.get(CONF_TV_WOL_MAC, DEFAULT_TV_WOL_MAC) or "").strip()
        if mac and self._tv_wol_block_reason(episode_id) is None:
            await self._svc("wake_on_lan", "send_magic_packet", {"mac": mac})
        _LOGGER.info("media_apply: R12 wake dispatched for screen episode %s", episode_id)

    # ----- Wake-Sequenz (R23) + bio-Flanken -----
    def _schedule_stop_latch_reset(
        self, bio_wake_edge: bool, inp: logic.Inputs
    ) -> None:
        """Clear the shared stop latch once on a known sleep-to-wake edge."""
        latch = self._entity_id(CONF_STOP_LATCH)
        if not (
            bio_wake_edge
            and self.apply_enabled
            and inp.stop_latch
            and latch
        ):
            return
        self._last_stop_latch_reset_reason = "bio_wake_edge"
        self._last_stop_latch_reset_at = dt_util.utcnow().isoformat()
        _LOGGER.info("media_apply: stop latch reset (bio_wake_edge)")
        self.hass.async_create_task(
            self._svc(
                "homeassistant",
                "turn_off",
                {"entity_id": latch},
                blocking=True,
            )
        )

    def _bio_edges(self) -> tuple[bool, bool]:
        """bio_state-Flanken (to_awake, to_sleep) aus core_state. EINMAL pro Tick
        (mutiert Vortick-State); Erststand zählt nicht. to_awake = Eintritt in
        awake/waking (Wake, R23); to_sleep = Eintritt in sleep (FLEET-98 private-Clear)."""
        cur = self._state(CONF_BIO_STATE)
        prev = self._last_bio_state
        self._last_bio_state = cur
        to_awake = prev is not None and prev not in BIO_AWAKE_VALUES and cur in BIO_AWAKE_VALUES
        to_sleep = prev is not None and prev != BIO_SLEEP_VALUE and cur == BIO_SLEEP_VALUE
        return to_awake, to_sleep

    def _wake_trigger_fired(self, bio_to_awake: bool) -> bool:
        """Wake-Flanke = bio→awake (primär) ODER steigende Flanke eines optionalen
        Roh-Triggers (Multi-Entity, Default leer). Nur EINMAL pro Tick aufrufen."""
        fired = bio_to_awake
        ents = self._entity_id(CONF_WAKE_TRIGGERS)
        if isinstance(ents, str):
            ents = [ents]
        if isinstance(ents, (list, tuple)):
            for eid in ents:
                if not isinstance(eid, str) or not eid:
                    continue
                st = self.hass.states.get(eid)
                raw = st.state if st and st.state not in ("unknown", "unavailable") else None
                cur = _bool(raw)
                if self._last_wake_states.get(eid) is False and cur is True:
                    fired = True
                self._last_wake_states[eid] = cur
        return fired

    @callback
    def _schedule_wake(self) -> None:
        self._cancel_wake()
        self._wake_task = self.hass.async_create_task(self._run_wake())

    @callback
    def _cancel_wake(self) -> None:
        if self._wake_task is not None and not self._wake_task.done():
            self._wake_task.cancel()
        self._wake_task = None

    async def _run_wake(self) -> None:
        """R23: HomePods auf Startlautstärke → Debounce → Ramp auf das aktuelle
        media_policy-Ziel (`volume_target_homepods`). Abbrechbar; nutzt die normale
        Ramp-Maschine für den Hochlauf."""
        # benni_media#16 — Wake-Volume ebenfalls PRO POD (nicht auf die Gruppe).
        hp = self._entity_id(CONF_HOMEPODS_PLAYER)
        pods = self._homepods_volume_targets()
        if not pods:
            return
        s = self.settings()
        start = round(max(0.0, min(1.0, s.wake_start_volume)), 3)
        try:
            self._cancel_ramp()
            # Race-Fix: Volume-Floor BLOCKIEREND setzen, damit er anliegt, bevor der
            # (auf derselben Wake-Flanke gestartete) Radio-Autostart Ton ausgibt.
            await self._svc(
                "media_player", "volume_set",
                {"entity_id": pods, "volume_level": start}, blocking=True,
            )
            await asyncio.sleep(max(0.0, s.wake_debounce_seconds))
            if logic.media_block_reason(self._build_inputs()):
                return
            wake_inputs = self._build_inputs()
            target = self._float(CONF_VOL_TARGET_HOMEPODS)
            if target is None:
                return
            effective_target = logic.wake_ramp_target(wake_inputs, start, target)
            levels = logic.ramp_levels(start, effective_target, s.ramp_steps, s.tiny_delta)
            if levels:
                self._cancel_ramp()
                self._ramp_task = self.hass.async_create_task(
                    self._run_ramp(pods, levels, s.ramp_step_delay_s)
                )
            _LOGGER.info(
                "media_apply: R23 Wake-Sequenz %s → %.2f → Ramp auf %.2f",
                hp,
                start,
                effective_target,
            )
        except asyncio.CancelledError:
            raise
        finally:
            self._wake_task = None

    # ----- Sleep-TV-Off (R24) -----
    def _consume_extend_edge(self) -> bool:
        """Flanke: hat sich der Lichtschalter-Taster-State seit dem letzten Tick
        geändert? (Druck = State-Change). Nur EINMAL pro Tick aufrufen (mutiert)."""
        cur = self._state(CONF_SLEEP_TV_EXTEND)
        pressed = (
            self._last_extend_state is not None
            and cur is not None
            and cur != self._last_extend_state
        )
        self._last_extend_state = cur
        return pressed

    @staticmethod
    def _epoch_iso(value: float | None) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()

    def _sleep_tv_evidence_attrs(self, evidence: str) -> dict[str, Any]:
        state = self._sleep_tv_state
        now = dt_util.utcnow().timestamp()
        return {
            "contract_version": SLEEP_TV_EVIDENCE_CONTRACT_VERSION,
            "evidence": evidence,
            "sleep_reference_start": state.sleep_reference_start,
            "deadline": self._epoch_iso(state.deadline),
            "timer_source": state.timer_source,
            "off_confirmed_since": self._epoch_iso(state.off_confirmed_since),
            "off_confirmed_at": self._epoch_iso(state.off_confirmed_at),
            "off_confirmation_seconds": DEFAULT_SLEEP_TV_OFF_CONFIRM,
            "off_confirmation_remaining_seconds": (
                round(max(0.0, state.off_confirmed_since + DEFAULT_SLEEP_TV_OFF_CONFIRM - now), 1)
                if state.off_confirmed_since is not None
                and state.off_confirmed_at is None
                else 0.0 if state.off_confirmed_at is not None else None
            ),
            "tv_state_quality": (
                "fresh" if self._powered(CONF_TV_POWER) is not None else "unavailable"
            ),
            "armed": state.armed,
            "off_commanded_for_deadline": self._epoch_iso(
                state.off_commanded_for_deadline
            ),
            "restart_safe": True,
        }

    @callback
    def _reconcile_sleep_tv_tasks(self) -> None:
        state = self._sleep_tv_state
        if (
            state.armed
            and state.deadline is not None
            and state.off_commanded_for_deadline != state.deadline
        ):
            self._schedule_sleep_tv(state.deadline)
        else:
            self._cancel_sleep_tv()
        if state.off_confirmed_since is not None and state.off_confirmed_at is None:
            self._schedule_sleep_tv_confirmation(
                state.off_confirmed_since + DEFAULT_SLEEP_TV_OFF_CONFIRM
            )
        else:
            self._cancel_sleep_tv_confirmation()

    @callback
    def _schedule_sleep_tv(self, deadline: float) -> None:
        if (
            self._sleep_tv_task is not None
            and not self._sleep_tv_task.done()
            and self._sleep_tv_task_deadline == deadline
        ):
            return
        self._cancel_sleep_tv()
        self._sleep_tv_task_deadline = deadline
        self._sleep_tv_task = self.hass.async_create_task(
            self._run_sleep_tv(deadline)
        )

    @callback
    def _cancel_sleep_tv(self) -> None:
        if self._sleep_tv_task is not None and not self._sleep_tv_task.done():
            self._sleep_tv_task.cancel()
        self._sleep_tv_task = None
        self._sleep_tv_task_deadline = None

    @callback
    def _schedule_sleep_tv_confirmation(self, deadline: float) -> None:
        if (
            self._sleep_tv_confirmation_task is not None
            and not self._sleep_tv_confirmation_task.done()
            and self._sleep_tv_confirmation_deadline == deadline
        ):
            return
        self._cancel_sleep_tv_confirmation()
        self._sleep_tv_confirmation_deadline = deadline
        self._sleep_tv_confirmation_task = self.hass.async_create_task(
            self._run_sleep_tv_confirmation(deadline)
        )

    @callback
    def _cancel_sleep_tv_confirmation(self) -> None:
        task = self._sleep_tv_confirmation_task
        if task is not None and not task.done():
            task.cancel()
        self._sleep_tv_confirmation_task = None
        self._sleep_tv_confirmation_deadline = None

    async def _run_sleep_tv_confirmation(self, deadline: float) -> None:
        try:
            await asyncio.sleep(max(0.0, deadline - dt_util.utcnow().timestamp()))
        except asyncio.CancelledError:
            raise
        self._sleep_tv_confirmation_task = None
        self._sleep_tv_confirmation_deadline = None
        self.async_set_updated_data(self._compute())

    async def _run_sleep_tv(self, deadline: float) -> None:
        """Reconcile one absolute deadline exactly once across HA restarts."""
        delay = self._duration(CONF_SLEEP_TV_OFF_DELAY, DEFAULT_SLEEP_TV_OFF_DELAY)
        lead = min(
            self._duration(CONF_SLEEP_TV_WARN_LEAD, DEFAULT_SLEEP_TV_WARN_LEAD),
            delay,
        )
        try:
            await asyncio.sleep(
                max(0.0, deadline - lead - dt_util.utcnow().timestamp())
            )
            if self._sleep_tv_state.deadline != deadline:
                return
            if (
                self.apply_enabled
                and self._sleep_tv_state.warned_for_deadline != deadline
            ):
                self._sleep_tv_state.warned_for_deadline = deadline
                await self._async_persist_sleep_tv()
                await self._sleep_tv_warn()
            await asyncio.sleep(max(0.0, deadline - dt_util.utcnow().timestamp()))
        except asyncio.CancelledError:
            raise
        self._sleep_tv_task = None
        self._sleep_tv_task_deadline = None
        if self._sleep_tv_state.deadline != deadline:
            return
        inp = self._build_inputs()
        if (
            inp.bio_state not in BIO_SLEEP_CONTEXT_VALUES
            or logic._sleep_tv_is_off(inp) is not False
        ):
            return
        if self._sleep_tv_state.off_commanded_for_deadline == deadline:
            return
        # Persist before the service call: a restart between dispatch and the
        # next TV state event cannot blindly duplicate the same deadline.
        self._sleep_tv_state.off_commanded_for_deadline = deadline
        self._sleep_tv_state.armed = False
        await self._async_persist_sleep_tv()
        if self.apply_enabled:
            tv = self._entity_id(CONF_TV_PLAYER)
            if tv:
                self._tv_wol_state.consume("r12:sleep_tv_off")
                _LOGGER.info("media_apply: R24 Sleep-TV-Off abgelaufen → turn_off %s", tv)
                await self._svc("media_player", "turn_off", {"entity_id": tv})
        else:
            _LOGGER.debug("media_apply: R24 Sleep-TV-Off abgelaufen (Shadow → kein Off)")
        self.async_set_updated_data(self._compute())

    async def _sleep_tv_warn(self) -> None:
        """TV-Warnung via konfiguriertem notify-Service (z.B. notify.living_lgtv).
        Leer/ohne Punkt → keine Warnung (degraded, schaltet trotzdem aus)."""
        svc = str(self._opts.get(CONF_SLEEP_TV_NOTIFY, DEFAULT_SLEEP_TV_NOTIFY) or "").strip()
        if "." not in svc:
            return
        domain, service = svc.split(".", 1)
        msg = self._opts.get(CONF_SLEEP_TV_WARN_MESSAGE) or DEFAULT_SLEEP_TV_WARN_MESSAGE
        await self._svc(domain, service, {"message": msg})

    # ----- Denon-Nachlauf (R13/R14) -----
    def _duration(self, key: str, default: float) -> float:
        try:
            return float(self._opts.get(key, default))
        except (TypeError, ValueError):
            return default

    def _hard_recovery_remaining(self) -> float:
        if self._last_hard_recovery_at is None:
            return 0.0
        cooldown = self._duration(
            CONF_MUSIC_ASSISTANT_RESTART_COOLDOWN,
            DEFAULT_MUSIC_ASSISTANT_RESTART_COOLDOWN,
        )
        return round(
            max(0.0, cooldown - (self.hass.loop.time() - self._last_hard_recovery_at)),
            2,
        )

    @callback
    def _apply_nachlauf(self, nplan: "logic.NachlaufPlan") -> None:
        self._dispatch_timer("pc", nplan.pc, CONF_DENON_NACHLAUF_PC, DEFAULT_DENON_NACHLAUF_PC)
        self._dispatch_timer("tv", nplan.tv, CONF_DENON_NACHLAUF_TV, DEFAULT_DENON_NACHLAUF_TV)

    @callback
    def _dispatch_timer(self, key: str, intent: str, conf: str, default: float) -> None:
        if intent == logic.TIMER_ARM:
            self._schedule_nachlauf(key, self._duration(conf, default))
        elif intent in (logic.TIMER_CANCEL, logic.TIMER_PAUSE):
            # PAUSE bricht nur den realen Countdown ab; das armed/paused-Buchwerk
            # hält die Pure-Logic (Resume = Neustart nach Sleep-Ende).
            self._cancel_nachlauf(key)

    @callback
    def _schedule_nachlauf(self, key: str, duration: float) -> None:
        self._cancel_nachlauf(key)
        self._nachlauf_tasks[key] = self.hass.async_create_task(
            self._run_nachlauf(key, duration)
        )

    # ----- control#3: Private-Exit-Denon-Off-Delay (separater Timer) -----
    @callback
    def _dispatch_private_exit(self, intent: str) -> None:
        if intent == logic.TIMER_ARM:
            self._cancel_nachlauf("private_exit")
            self._nachlauf_tasks["private_exit"] = self.hass.async_create_task(
                self._run_private_exit(
                    self._duration(CONF_PRIVATE_EXIT_DELAY, DEFAULT_PRIVATE_EXIT_DELAY)
                )
            )
        elif intent == logic.TIMER_CANCEL:
            self._cancel_nachlauf("private_exit")

    async def _run_private_exit(self, duration: float) -> None:
        """Wartet den Private-Exit-Delay ab, dann (gegatet) Denon aus. Abbrechbar
        via _cancel_nachlauf('private_exit') (TV/Private/Konsument-Flanke)."""
        try:
            await asyncio.sleep(max(0.0, duration))
        except asyncio.CancelledError:
            raise
        self._nachlauf_tasks.pop("private_exit", None)
        self._private_exit_state.armed = False   # self-heal vor dem nächsten Tick
        if self.apply_enabled:
            if self._denon_consumer_holds() is True:
                _LOGGER.info(
                    "media_apply: Private-Exit-Delay abgelaufen, aber Denon-"
                    "Konsument aktiv → kein Off"
                )
            else:
                await self._denon_power_off("private_exit")
        else:
            _LOGGER.debug(
                "media_apply: Private-Exit-Delay abgelaufen (Shadow → kein Denon-Off)"
            )
        if self.data is not None:
            self.async_set_updated_data(dict(self.data))

    @callback
    def _cancel_nachlauf(self, key: str) -> None:
        task = self._nachlauf_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    async def _run_nachlauf(self, key: str, duration: float) -> None:
        """Wartet `duration` Sekunden, dann (gegatet) Denon aus. Abbrechbar:
        PC/TV zurück oder Sleep (R14) canceln den Task vorher."""
        try:
            await asyncio.sleep(max(0.0, duration))
        except asyncio.CancelledError:
            raise
        self._nachlauf_tasks.pop(key, None)
        # Armed-Flag proaktiv löschen (self-heal vor dem nächsten Tick).
        if key == "pc":
            self._nachlauf_state.pc_armed = False
        else:
            self._nachlauf_state.tv_armed = False
            self._nachlauf_state.tv_paused = False
        if self.apply_enabled:
            # FLEET-80: finaler Konsumenten-Check am Ablauf (event-getriebener
            # Cancel sollte schon gegriffen haben — doppelt safe gegen Races).
            if self._denon_consumer_holds() is True:
                _LOGGER.info(
                    "media_apply: Nachlauf %s abgelaufen, aber Denon-Konsument "
                    "aktiv (media_device) → kein Off", key
                )
            else:
                await self._denon_power_off(key)
        else:
            _LOGGER.debug(
                "media_apply: Nachlauf %s abgelaufen (Shadow → kein Denon-Off)", key
            )
        if self.data is not None:
            self.async_set_updated_data({
                **self.data,
                "denon_nachlauf_active": (
                    self._nachlauf_state.pc_armed or self._nachlauf_state.tv_armed
                ),
            })

    async def _denon_power_off(self, key: str) -> None:
        denon = self._entity_id(CONF_DENON_PLAYER)
        if not denon:
            return
        _LOGGER.info("media_apply: Denon-Nachlauf %s abgelaufen → turn_off %s", key, denon)
        await self._svc("media_player", "turn_off", {"entity_id": denon})

    # ----- Radio-Shortcuts (manuell, Phase 4b) -----
    def _ma_config_entry_id(self) -> Optional[str]:
        """Config-Entry der Music-Assistant-Integration (für den Search-Service)."""
        for entry in self.hass.config_entries.async_entries("music_assistant"):
            return entry.entry_id
        return None

    async def async_play_radio(self, media_id: str) -> dict[str, Any]:
        """MANUELL einen Sender abspielen (Cockpit-Shortcut / Suchtreffer).

        Bewusster User-Befehl → spielt SOFORT, **unabhängig vom Shadow-Gate**
        (`apply_enabled`); nur der automatische Policy-Apply ist shadow-gated.
        `media_id` ist eine MA-URI (radiobrowser://, library://, …)."""
        hp = self._entity_id(CONF_HOMEPODS_PLAYER)
        if not hp:
            raise ValueError("HomePods-Player nicht gebunden")
        if not media_id:
            raise ValueError("media_id fehlt")
        await self._svc(
            "music_assistant", "play_media",
            {
                "entity_id": hp, "media_id": media_id,
                "media_type": RADIO_MEDIA_TYPE, "enqueue": RADIO_ENQUEUE,
            },
            blocking=True,
        )
        return {"played": media_id, "target": hp}

    async def async_search_radio(self, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Radiosender über Music Assistant suchen → normalisierte Trefferliste
        [{name, uri, image, favorite}]. Leere/keine Treffer → []."""
        query = (query or "").strip()
        if not query:
            return []
        entry_id = self._ma_config_entry_id()
        if not entry_id:
            raise ValueError("music_assistant nicht geladen")
        lim = int(limit or DEFAULT_RADIO_SEARCH_LIMIT)
        try:
            resp = await self.hass.services.async_call(
                "music_assistant", "search",
                {"config_entry_id": entry_id, "name": query,
                 "media_type": ["radio"], "limit": lim},
                blocking=True, return_response=True,
            )
        except Exception as err:  # noqa: BLE001 — Suche darf das Cockpit nicht crashen
            _LOGGER.warning("media_apply: radio search '%s' failed: %s", query, err)
            return []
        radio = (resp or {}).get("radio") or []
        out: list[dict[str, Any]] = []
        for item in radio:
            if not isinstance(item, dict) or not item.get("uri"):
                continue
            out.append({
                "name": item.get("name") or item["uri"],
                "uri": item["uri"],
                "image": item.get("image"),
                "favorite": bool(item.get("favorite")),
            })
        return out

    async def async_radio_shortcuts(self) -> list[dict[str, Any]]:
        """Default-Sender mit Music-Assistant-Logo und Live-Markierung."""
        if self._radio_shortcuts_cache is None:
            enriched: list[dict[str, Any]] = []
            for station in logic.radio_defaults():
                matches = await self.async_search_radio(station["name"], 5)
                match = next((item for item in matches if item["uri"] == station["uri"]), None)
                if match is None:
                    target = station["name"].casefold()
                    match = next((item for item in matches if str(item["name"]).casefold() == target), None)
                enriched.append({**station, "image": (match or {}).get("image"), "favorite": bool((match or {}).get("favorite"))})
            self._radio_shortcuts_cache = enriched
        inp = self._build_inputs()
        return [
            {**station, "playing": bool(inp.planned_station_playing and inp.radio_station == station.get("key"))}
            for station in self._radio_shortcuts_cache
        ]

    # ----- service surface -----
    async def async_set_apply_enabled(self, value: bool) -> None:
        """Apply zur Laufzeit an/aus. Schreibt in die Options → Reload-Listener."""
        new_options = {**self.entry.options, CONF_APPLY_ENABLED: bool(value)}
        self.hass.config_entries.async_update_entry(self.entry, options=new_options)
