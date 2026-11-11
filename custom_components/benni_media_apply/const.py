"""Konstanten von benni_media_apply (Ausführungsschicht / Executor).

Eigenständige HA-Integration. Konsumiert benni_media_state (Szenario) +
benni_media_policy (Targets/Action/Gates) AUSSCHLIESSLICH über HA-Entity-State —
kein Cross-Modul-Python-Import (Contracts werden KOPIERT, nicht importiert).

Phase 1 (FLEET-40): Kern-Apply — HomePods-Action (pause/play; start_radio
delegiert an script.media_radio_start), Volume (HomePods geramped, Denon hart),
Subwoofer on/off. Apply-Gate: apply_enabled (Option, Shadow-safe OFF) ×
volume_apply_allowed (pro Entscheidung, aus media_policy).

Lastenheft: einhornzentrale/docs/lastenhefte/reviewed/media/ (v3.1)
"""
from __future__ import annotations

from typing import Any, Final

DOMAIN: Final[str] = "benni_media_apply"
MODULE_ID: Final[str] = "media_apply"
NAME: Final[str] = "Benni Media Apply"

DATA_COORDINATOR: Final[str] = "coordinator"
SLEEP_TV_STORAGE_VERSION: Final[int] = 1
SLEEP_TV_EVIDENCE_CONTRACT_VERSION: Final[str] = "1.0.0"


def sleep_tv_storage_key(entry_id: str) -> str:
    return f"{DOMAIN}_sleep_tv_{entry_id}"

# Panel / WebSocket-API. Der WS-Contract ist das Bleibende (Umbrella-fähig),
# das Panel ist Wegwerf (folgt). Read frei, Schreiben Admin.
WS_GET_STATUS: Final[str] = f"{DOMAIN}/get_status"
WS_SET_APPLY_ENABLED: Final[str] = f"{DOMAIN}/set_apply_enabled"
WS_SCHEDULE_REAPPLY: Final[str] = f"{DOMAIN}/schedule_reapply"
WS_APPLY_REAPPLY_NOW: Final[str] = f"{DOMAIN}/apply_reapply_now"
WS_CANCEL_REAPPLY: Final[str] = f"{DOMAIN}/cancel_reapply"

# Panel (Wegwerf-Frontend auf dem WS-Contract).
DATA_VIEW_PANEL: Final[str] = "_view_panel"
DATA_VIEW_STATIC: Final[str] = "_view_static"
PANEL_URL_PATH: Final[str] = "benni_media_apply"
PANEL_TITLE: Final[str] = "Media Apply"
PANEL_ICON: Final[str] = "mdi:cast-audio-variant"
PANEL_ELEMENT: Final[str] = "bma-app"
FRONTEND_DIR_URL: Final[str] = "/benni_media_apply_app"
FRONTEND_ENTRY: Final[str] = f"{FRONTEND_DIR_URL}/main.js"


def unique_id(entry_id: str, suffix: str) -> str:
    """Domain- + entry-scoped unique_id (core_state-Blaupause, kollisionsfrei)."""
    return f"{DOMAIN}_{entry_id}_{suffix}"


# --------------------------------------------------------------------------- #
# Profil-Hub (benni / eltern) + Auto-Bind: options ▶ data ▶ Profil-Map ▶ leer.
# --------------------------------------------------------------------------- #
CONF_PROFILE: Final[str] = "profile"
PROFILE_BENNI: Final[str] = "benni"
PROFILE_ELTERN: Final[str] = "eltern"
PROFILES: Final[list[str]] = [PROFILE_BENNI, PROFILE_ELTERN]
DEFAULT_PROFILE: Final[str] = PROFILE_BENNI
PROFILE_LABELS: Final[dict[str, str]] = {PROFILE_BENNI: "Benni", PROFILE_ELTERN: "Eltern"}

# --------------------------------------------------------------------------- #
# Action-Contract (KOPIE aus media_policy — kein Import).
# --------------------------------------------------------------------------- #
ACTION_NONE: Final = "none"
ACTION_PAUSE: Final = "pause_homepods"
ACTION_RESUME: Final = "resume_homepods"
ACTION_START_RADIO: Final = "start_radio"
ACTION_DENON_OFF: Final = "turn_off_denon"

# Ausführungs-Modus pro Tick (R2/R3): wie der berechnete Plan zum Gerät kommt.
EXEC_SHADOW: Final = "shadow"        # apply_enabled aus → gar nicht ausführen.
EXEC_IMMEDIATE: Final = "immediate"  # Quiet bricht sofort durch (kein Debounce).
EXEC_DEBOUNCE: Final = "debounce"    # Normalfall → 5s-Fenster konsolidiert Bursts.

# media_player-Zustände, die als "spielt" gelten.
PLAYER_PLAYING_VALUES: Final = ("playing",)
# Zustände, in denen ein Volume-Set sinnvoll ist (nicht unknown/unavailable/off).
PLAYER_ADDRESSABLE_VALUES: Final = ("playing", "idle", "paused", "on", "buffering")
# media_player/AVR-Zustände, die als "ausgeschaltet" gelten (Denon-Power-Ableitung,
# falls keine dedizierte Denon-Power-Quelle gebunden ist). Gilt auch für die TV-Power
# (R11: WebOS off/standby = aus).
PLAYER_OFF_VALUES: Final = ("off", "standby")

# R12 — Bildschirm-Szenarien: media_device-Werte, die den TV als Output brauchen
# (TV direkt + Apple TV via HDMI). Bei Wechsel hierauf + TV aus → TV einschalten.
SCREEN_DEVICES: Final = ("tv", "appletv")

# R13/R14 — Denon-Konsumenten (media_device-Werte, die den Denon als Audio-Senke
# brauchen). FLEET-80 Cross-Source-Gate: Solange media_device hier drin ist, ist
# der geteilte Denon in Benutzung und der Nachlauf darf ihn NICHT ausschalten.
# „denon" (Self — denon_active ist beim Abschalten per Definition true) sowie
# „homepods" (separate Audio-Senke) und „none" zählen bewusst NICHT als Konsument.
DENON_CONSUMER_DEVICES: Final = ("tv", "appletv", "ps5", "switch", "pc")

# benni_media#14 — Konsumenten, für die media_apply eine EIGENE, autoritative
# Power-Quelle gebunden hat (PC-/TV-Master bzw. WebOS-State). Nur für diese darf
# ein explizites „aus" das beschreibende `media_device`-Label überstimmen; alle
# anderen (appletv/ps5/switch) bleiben konservativ, weil es dafür keine
# unabhängige Power-Wahrheit gibt.
DEV_LABEL_PC: Final = "pc"
DEV_LABEL_TV: Final = "tv"
DENON_CONSUMER_POWER_CHECKED: Final = (DEV_LABEL_PC, DEV_LABEL_TV)

# Bio-State (core_state), bei dem R14 pausiert (Sleep dominant).
BIO_SLEEP_VALUE: Final = "sleep"
BIO_PROVISIONAL_SLEEP_VALUE: Final = "provisional_sleep"
BIO_SLEEP_CONTEXT_VALUES: Final = (BIO_PROVISIONAL_SLEEP_VALUE, BIO_SLEEP_VALUE)
# R23 — Wake-Übergang: bio_state-Werte, die als „wach" gelten. Der Eintritt in
# diese Menge (aus einem Nicht-Wach-Zustand) ist der primäre Wake-Trigger — Quelle
# ist core_state (KEINE Doppel-Detektion der Indikatoren). `waking` wie `awake` (KH-4).
BIO_AWAKE_VALUES: Final = ("awake", "waking")

# --------------------------------------------------------------------------- #
# Config-Keys — Eingänge (via Entity-State).
# --------------------------------------------------------------------------- #
# aus media_policy:
CONF_AUDIO_OWNER: Final[str] = "audio_owner_entity"
CONF_ACTION: Final[str] = "action_entity"
CONF_VOLUME_POLICY: Final[str] = "volume_policy_entity"
CONF_VOL_TARGET_HOMEPODS: Final[str] = "volume_target_homepods_entity"
CONF_VOL_TARGET_DENON: Final[str] = "volume_target_denon_entity"
CONF_HOMEPODS_SHOULD_PAUSE: Final[str] = "homepods_should_pause_entity"
CONF_HOMEPODS_RESUME_ALLOWED: Final[str] = "homepods_resume_allowed_entity"
CONF_SUBWOOFER_ALLOWED: Final[str] = "subwoofer_allowed_entity"
CONF_VOLUME_APPLY_ALLOWED: Final[str] = "volume_apply_allowed_entity"
# aus media_state:
CONF_QUIET_MODE: Final[str] = "quiet_mode_entity"
CONF_PRESENCE_STATE: Final[str] = "presence_state_entity"
CONF_AWAY_GATE: Final[str] = "away_gate_entity"
# Stop-Latch (shared Helper):
CONF_STOP_LATCH: Final[str] = "stop_latch_entity"
# Radio (Phase 4b — Katalog-Port aus script.media_radio_start):
CONF_RADIO_STATION: Final[str] = "radio_station_entity"        # input_select (Sender-Key)
CONF_RADIO_READY: Final[str] = "radio_ready_entity"            # binary_sensor (Sender gültig)
CONF_MANUAL_PLAYBACK: Final[str] = "manual_playback_entity"    # binary_sensor (manuell aktiv)
CONF_PLANNED_STATION_PLAYING: Final[str] = "planned_station_playing_entity"  # binary_sensor (geplante Station läuft)
# FLEET-44/98: Der manuelle private_time-Latch + seine Auto-Löschung leben jetzt
# nativ in media_state (switch-Entität) — apply verwaltet ihn nicht mehr.
# Geräte (Apply-Targets):
CONF_HOMEPODS_PLAYER: Final[str] = "homepods_player_entity"
# benni_media#16 — Volume geht PRO POD, nicht auf die Gruppe (Lastenheft
# „pro Gerät einzeln, kein Gruppen-Call"): ein `volume_set` auf die AirPlay-Sync-
# GRUPPE weckt einen pausierten Verbund wieder auf, auf die einzelnen Pods nicht.
# Pause/Resume/Radio laufen weiter auf der Gruppe (CONF_HOMEPODS_PLAYER); nur der
# Volume-Set/Ramp adressiert diese Pod-Liste. Leer/ungebunden ⇒ Fallback Gruppe.
CONF_HOMEPODS_PODS: Final[str] = "homepods_pod_entities"
CONF_DENON_PLAYER: Final[str] = "denon_player_entity"
CONF_SUBWOOFER_SWITCH: Final[str] = "subwoofer_switch_entity"

# --------------------------------------------------------------------------- #
# Phase 3 (R13/R14 Denon-Nachlauf) — Geräte-Power-Inputs.
# PC-/TV-Power kommen aus Core-Devices-Mastern. _powered() liest bevorzugt
# `powered`, dann `is_active`/`watt_active`, dann den bool-kompatiblen State.
# Denon-Power leitet sich notfalls aus dem bereits gebundenen CONF_DENON_PLAYER
# ab; bio_state kommt aus core_state (stabil).
# --------------------------------------------------------------------------- #
CONF_PC_POWER: Final[str] = "pc_power_entity"
CONF_TV_POWER: Final[str] = "tv_power_entity"
CONF_DENON_POWER: Final[str] = "denon_power_entity"
CONF_BIO_STATE: Final[str] = "bio_state_entity"
# R12 — TV-WoL: aktives Output-Gerät (media_state) + TV-Player (turn_on + WebOS-State).
CONF_MEDIA_DEVICE: Final[str] = "media_device_entity"
CONF_TV_PLAYER: Final[str] = "tv_player_entity"
# R24 — Sleep-TV-Off: Lichtschalter-Taste, deren Druck (State-Change) den Timer
# um eine Runde verlängert. Optional; ungebunden = keine Verlängerung möglich.
CONF_SLEEP_TV_EXTEND: Final[str] = "sleep_tv_extend_entity"
# R23 — Wake-Sequenz: Liste der Wake-Trigger-Entities (Kaffeemaschine, Fenster,
# PS5-Ein, PC-Ein, Private-Time). Steigende Flanke EINER davon startet die Sequenz.
# Bewusst NICHT TV/ATV (Schutz vor Fernbedienungs-Klick im Schlaf).
CONF_WAKE_TRIGGERS: Final[str] = "wake_trigger_entities"

# Keys, deren gebundene Entities der Coordinator beobachtet (event-driven).
WATCH_KEYS: Final[tuple[str, ...]] = (
    CONF_AUDIO_OWNER, CONF_ACTION, CONF_VOLUME_POLICY,
    CONF_VOL_TARGET_HOMEPODS, CONF_VOL_TARGET_DENON,
    CONF_HOMEPODS_SHOULD_PAUSE, CONF_HOMEPODS_RESUME_ALLOWED,
    CONF_SUBWOOFER_ALLOWED, CONF_VOLUME_APPLY_ALLOWED,
    CONF_QUIET_MODE, CONF_PRESENCE_STATE, CONF_AWAY_GATE, CONF_STOP_LATCH,
    CONF_RADIO_STATION, CONF_RADIO_READY, CONF_MANUAL_PLAYBACK,
    CONF_PLANNED_STATION_PLAYING,
    CONF_HOMEPODS_PLAYER, CONF_DENON_PLAYER, CONF_SUBWOOFER_SWITCH,
    CONF_PC_POWER, CONF_TV_POWER, CONF_DENON_POWER, CONF_BIO_STATE,
    CONF_MEDIA_DEVICE, CONF_TV_PLAYER, CONF_SLEEP_TV_EXTEND,
    CONF_WAKE_TRIGGERS,
)
# benni_media#16 — die Pods sind ein Config-Slot, werden aber NICHT beobachtet:
# der Ramp setzt ihre Lautstärke, und ein State-Watch darauf würde pro Schritt
# einen Recompute (Selbst-Trigger-Loop) auslösen. Darum nur im Schema, nicht in
# WATCH_KEYS.
ENTITY_SLOT_KEYS: Final[tuple[str, ...]] = WATCH_KEYS + (CONF_HOMEPODS_PODS,)

# --------------------------------------------------------------------------- #
# Profil-Map (Auto-Bind). benni = Live-IDs der Einhornzentrale. Existenz-Filter
# regelt Fehlendes. eltern leer (Anlage existiert noch nicht).
# --------------------------------------------------------------------------- #
PROFILE_PREFILL: Final[dict[str, dict[str, Any]]] = {
    PROFILE_BENNI: {
        CONF_AUDIO_OWNER: "sensor.benni_media_policy_audio_owner",
        CONF_ACTION: "sensor.benni_media_policy_action",
        CONF_VOLUME_POLICY: "sensor.benni_media_policy_volume_policy",
        CONF_VOL_TARGET_HOMEPODS: "sensor.benni_media_policy_volume_target_homepods",
        CONF_VOL_TARGET_DENON: "sensor.benni_media_policy_volume_target_denon",
        CONF_HOMEPODS_SHOULD_PAUSE: "binary_sensor.benni_media_policy_homepods_should_pause",
        CONF_HOMEPODS_RESUME_ALLOWED: "binary_sensor.benni_media_policy_homepods_resume_allowed",
        CONF_SUBWOOFER_ALLOWED: "binary_sensor.benni_media_policy_subwoofer_allowed",
        CONF_VOLUME_APPLY_ALLOWED: "binary_sensor.benni_media_policy_volume_apply_allowed",
        CONF_QUIET_MODE: "binary_sensor.benni_media_state_quiet_mode",
        # Live existiert (renamed-device, system_-Präfix) NUR der system_-Slug —
        # der clean slug existierte nie → presence_state/away_gate lösten zu None auf.
        CONF_PRESENCE_STATE: "sensor.system_benni_media_state_presence_state",
        CONF_AWAY_GATE: "binary_sensor.system_benni_media_state_away_gate",
        CONF_STOP_LATCH: "input_boolean.media_stop_latch",
        CONF_RADIO_STATION: "input_select.media_radio_station",
        CONF_RADIO_READY: "binary_sensor.media_radio_ready",
        CONF_MANUAL_PLAYBACK: "binary_sensor.media_manual_playback_active",
        CONF_PLANNED_STATION_PLAYING: "binary_sensor.media_radio_playing_planned_station",
        CONF_HOMEPODS_PLAYER: "media_player.living_homepods_ma_group",
        # benni_media#16 — die einzelnen AirPlay-Pods der Gruppe (Black defekt/
        # entfernt → aktuell 2). Volume/Ramp adressiert diese, nicht die Gruppe.
        CONF_HOMEPODS_PODS: [
            "media_player.living_homepod_blue_ma_airplay",
            "media_player.living_homepod_grey_ma_airplay",
        ],
        CONF_DENON_PLAYER: "media_player.living_denon",
        CONF_SUBWOOFER_SWITCH: "switch.living_subwoofer_plug",
        # Post-FLEET-54: an Core-Devices-Master/core_state gebunden
        # (Denon-Nachlauf R13/R14).
        CONF_PC_POWER: "sensor.benni_master_pc",
        CONF_TV_POWER: "sensor.benni_master_tv",
        # Watt-primärer Master (is_active), konsistent mit pc/tv. Der denonavr-Player
        # meldet im Betrieb oft stale "off" (assumed-state, FLEET-83-Muster) → watt
        # ist die Power-Wahrheit für Apply-Gate (Volume) UND Nachlauf (R13/R14).
        CONF_DENON_POWER: "sensor.benni_master_denon",
        CONF_BIO_STATE: "sensor.benni_core_state_bio_state",
        # R12 — TV-WoL.
        CONF_MEDIA_DEVICE: "sensor.benni_media_state_media_device",
        CONF_TV_PLAYER: "media_player.living_lgtv",
        # R23 — Primärer Wake-Trigger ist der bio_state-Übergang → awake/waking
        # (CONF_BIO_STATE, core_state). Diese Liste ist nur ein OPTIONALer Zusatz
        # (z.B. Private-Time-Helper), Default leer → keine Indikator-Doppel-Detektion.
        CONF_WAKE_TRIGGERS: [],
    },
    PROFILE_ELTERN: {},
}

LEGACY_ENTITY_MAP: Final[dict[str, str]] = {
    "sensor.benni_device_living_pc": "sensor.benni_master_pc",
    "sensor.benni_device_living_tv": "sensor.benni_master_tv",
    # Renamed-device (system_) Entity-ID-Fix: media_states presence_state/away_gate
    # existieren live nur als system_-Slug; alte gebackene clean-Slugs repointen.
    "sensor.benni_media_state_presence_state": "sensor.system_benni_media_state_presence_state",
    "binary_sensor.benni_media_state_away_gate": "binary_sensor.system_benni_media_state_away_gate",
}

# --------------------------------------------------------------------------- #
# Options — Apply-Gate + Ramp-Settings (§6, konfigurierbar).
# --------------------------------------------------------------------------- #
CONF_APPLY_ENABLED: Final[str] = "apply_enabled"
DEFAULT_APPLY_ENABLED: Final[bool] = False   # Shadow-safe out of the box.

CONF_RAMP_STEPS: Final[str] = "ramp_steps"
CONF_RAMP_STEP_DELAY: Final[str] = "ramp_step_delay_seconds"
CONF_TINY_DELTA: Final[str] = "tiny_delta"
CONF_DUCKED_LEVEL: Final[str] = "ducked_level"
# R2 — Debounce: Szenario-Übergänge warten dieses Fenster, Trigger-Bursts werden
# zu EINER Aktion konsolidiert. Quiet bricht durch (kein Debounce). 5s kalibrierbar.
CONF_DEBOUNCE_SECONDS: Final[str] = "debounce_seconds"
# benni_media#13 — Anti-Starvation-Deckel: jeder Plan mit Arbeit stieß das
# Debounce-Fenster bisher NEU an. Ein Übergangs-Burst konnte die Ausführung
# dadurch unbegrenzt vor sich herschieben. Ab diesem Alter des laufenden Fensters
# wird `latest-wins` weiter gepuffert, das Fenster aber NICHT mehr verlängert.
CONF_DEBOUNCE_MAX_WAIT: Final[str] = "debounce_max_wait_seconds"
# benni_media#13 — Der Denon kann technisch KEINE sinnvolle Volume-Rampe fahren
# (harter Set, sofort hörbar am AVR-OSD). Sein Volume-Set wartet deshalb NICHT
# auf das R2-Fenster, sondern geht sofort raus, sobald Zielkontext und Zielwert
# feststehen. Die HomePods-Rampe bleibt davon unberührt (bewusst sanft, 16×1s).
# Ausschaltbar, falls sich am AVR doch ein Flacker-Effekt zeigt.
CONF_DENON_IMMEDIATE: Final[str] = "denon_immediate_volume"
# Service-Delegation für start_radio — Fallback, wenn kein URI auflösbar
# (Sender ungebunden/unbekannt). Phase 4b portiert den Katalog inline.
CONF_RADIO_START_SCRIPT: Final[str] = "radio_start_script"
# Legacy-Option aus <= 0.19.4. Music Assistants play_media startet die Wiedergabe
# selbst; der nachgelagerte media_play-Pfad wurde in 0.19.5 entfernt. Der Key
# bleibt nur erhalten, damit bestehende Config-Entries ohne Migration lesbar sind.
CONF_RADIO_PLAY_DELAY: Final[str] = "radio_play_delay_seconds"

DEFAULT_RAMP_STEPS: Final[int] = 16
DEFAULT_RAMP_STEP_DELAY: Final[float] = 1.0
DEFAULT_TINY_DELTA: Final[float] = 0.02
DEFAULT_DUCKED_LEVEL: Final[float] = 0.10
DEFAULT_DEBOUNCE_SECONDS: Final[float] = 5.0
DEFAULT_DEBOUNCE_MAX_WAIT: Final[float] = 8.0
DEFAULT_DENON_IMMEDIATE: Final[bool] = True
DEFAULT_RADIO_START_SCRIPT: Final[str] = "script.media_radio_start"
DEFAULT_RADIO_PLAY_DELAY: Final[float] = 2.0
# Automatic radio dispatch protection: a failed provider must not be retriggered
# by every HomePod state flap. Manual cockpit playback deliberately bypasses it.
DEFAULT_RADIO_DISPATCH_COOLDOWN: Final[float] = 15.0
DEFAULT_RADIO_DISPATCH_MAX_BACKOFF: Final[float] = 120.0

# Radio-Katalog (Phase 4b) — Sender-Key → radiobrowser-URI. KOPIE aus dem
# YAML-Script media_radio_start (Strangler: inline statt Script-Delegation).
# Keys = Optionen von input_select.media_radio_station.
RADIO_CATALOG: Final[dict[str, str]] = {
    "1live": "radiobrowser://radio/b2cbd1fd-275d-432a-8b20-37dcb3572315",
    "wdr2_bergisches_land": "radiobrowser://radio/960c0309-0601-11e8-ae97-52543be04c81",
    "gayfm": "radiobrowser://radio/c3776781-8b87-414c-9f03-a751fc447fc6",
    "ndr1_niedersachsen": "radiobrowser://radio/98ff7177-c10d-4288-b1e3-9fbd4965c51e",
    "wdr4": "radiobrowser://radio/ccdcc4a2-9a79-41d4-8ca1-ff3d9c54a6a2",
    "jack_fm_berlin": "radiobrowser://radio/f39ed8f8-1b01-411c-a3e4-0a1f42124e72",
}
# Keep the existing selector key for restored choices. MA resolves provider
# domains to the loaded instance; no private TuneIn instance ID belongs here.
DEFAULT_RADIO_STATION: Final[str] = "jack_fm_berlin"
RADIO_FALLBACK_CATALOG: Final[dict[str, str]] = {
    "1live": "tunein://radio/s25260",
    "wdr2_bergisches_land": "tunein://radio/s99174",
    "gayfm": "tunein://radio/s96482",
    "ndr1_niedersachsen": "tunein://radio/s25529",
    "wdr4": "tunein://radio/s324638",
    "jack_fm_berlin": "tunein://radio/s84493",
}
RADIO_MEDIA_TYPE: Final[str] = "radio"
RADIO_ENQUEUE: Final[str] = "replace"

# Radio-Autostart (FLEET-79, Port der disabled YAML-Automationen). Trigger A:
# Wake-Flanke → Latch lösen + geplante Station starten. Trigger B: manuelle
# Wiedergabe endet → nach Delay geplante Station fortsetzen.
CONF_RADIO_AUTOSTART: Final[str] = "radio_autostart_enabled"
CONF_RADIO_RESUME_DELAY: Final[str] = "radio_resume_delay_seconds"
DEFAULT_RADIO_AUTOSTART: Final[bool] = True
DEFAULT_RADIO_RESUME_DELAY: Final[float] = 10.0

# Wake-Playback-Recovery (#41). Ein Wake-Start ist single-flight; nach dem
# initialen Music-Assistant-Start prüft der Coordinator Gruppe + einzelne Pods.
# Soft-Recovery ist standardmäßig aktiv. Ein Add-on/App-Neustart ist bewusst
# opt-in und braucht eine explizite App-ID — kein privater Slug im Public Repo.
CONF_PLAYBACK_RECOVERY: Final[str] = "playback_recovery_enabled"
CONF_PLAYBACK_RECOVERY_SETTLE: Final[str] = "playback_recovery_settle_seconds"
CONF_PLAYBACK_RECOVERY_HARD_AFTER: Final[str] = "playback_recovery_hard_after_seconds"
CONF_PLAYBACK_HARD_RECOVERY: Final[str] = "playback_hard_recovery_enabled"
CONF_MUSIC_ASSISTANT_APP_ID: Final[str] = "music_assistant_app_id"
CONF_MUSIC_ASSISTANT_RESTART_WAIT: Final[str] = "music_assistant_restart_wait_seconds"
CONF_MUSIC_ASSISTANT_RESTART_COOLDOWN: Final[str] = "music_assistant_restart_cooldown_seconds"

DEFAULT_PLAYBACK_RECOVERY: Final[bool] = True
DEFAULT_PLAYBACK_RECOVERY_SETTLE: Final[float] = 60.0
DEFAULT_PLAYBACK_RECOVERY_HARD_AFTER: Final[float] = 300.0
DEFAULT_PLAYBACK_RECOVERY_RECHECK: Final[float] = 30.0
DEFAULT_PLAYBACK_HEALTH_SAMPLES: Final[int] = 3
DEFAULT_PLAYBACK_HEALTH_SAMPLE_INTERVAL: Final[float] = 5.0
DEFAULT_STUCK_MUTE_RETRY_ATTEMPTS: Final[int] = 2
DEFAULT_STUCK_MUTE_RETRY_DELAY: Final[float] = 2.0
DEFAULT_STUCK_MUTE_EVENT_COOLDOWN: Final[float] = 30.0
DEFAULT_STUCK_MUTE_BACKSTOP_INTERVAL: Final[float] = 1800.0
DEFAULT_PLAYBACK_HARD_RECOVERY: Final[bool] = False
DEFAULT_MUSIC_ASSISTANT_APP_ID: Final[str] = ""
DEFAULT_MUSIC_ASSISTANT_RESTART_WAIT: Final[float] = 60.0
DEFAULT_MUSIC_ASSISTANT_RESTART_COOLDOWN: Final[float] = 1800.0

# Anzeige-Namen der Default-Sender (für Shortcuts im Cockpit). Kopie aus
# sensor.media_radio_plan (station_name). Keys = RADIO_CATALOG-Keys.
RADIO_STATION_LABELS: Final[dict[str, str]] = {
    "1live": "1LIVE",
    "wdr2_bergisches_land": "WDR 2 Bergisches Land",
    "gayfm": "GAYFM",
    "ndr1_niedersachsen": "NDR 1 Niedersachsen",
    "wdr4": "WDR 4 Rhein und Ruhr",
    "jack_fm_berlin": "Jack FM Deutschland",
}
# Exact provider names only; never fuzzy-match a different country/region.
RADIO_SEARCH_NAMES: Final[dict[str, tuple[str, ...]]] = {
    "jack_fm_berlin": ("Jack FM Deutschland", "Jack FM"),
    "gayfm": ("GAYFM", "GAY FM - Pure Dance!", "Gay FM"),
    "wdr4": ("WDR 4 Rhein und Ruhr", "WDR 4"),
}
# MA-Suche (Phase 4b — „andere Sender"). Default-Trefferzahl.
DEFAULT_RADIO_SEARCH_LIMIT: Final[int] = 10

# Phase 3 — Denon-Nachlauf (R13/R14), Sekunden (Lastenheft 20_helpers: 90s).
CONF_DENON_NACHLAUF_PC: Final[str] = "denon_nachlauf_pc_seconds"
CONF_DENON_NACHLAUF_TV: Final[str] = "denon_nachlauf_tv_seconds"
DEFAULT_DENON_NACHLAUF_PC: Final[float] = 90.0
DEFAULT_DENON_NACHLAUF_TV: Final[float] = 90.0

# control#3 — Private-Time-Exit-Denon-Off-Delay (separat vom 90 s-Nachlauf).
# audio_owner-Wert, der Private Time markiert (Kopie des media_policy-Contracts).
AUDIO_OWNER_PRIVATE: Final[str] = "private_stack"
CONF_PRIVATE_EXIT_DELAY: Final[str] = "private_exit_delay_seconds"
DEFAULT_PRIVATE_EXIT_DELAY: Final[float] = 15.0

# Phase 4c — TV-WoL (R12). media_player.turn_on löst das webOS-„Leuchtfeuer" aus
# (bleibt 24/7 aktiv, die LG-Integration braucht es für den On/Off-Status); ist
# zusätzlich eine MAC gesetzt, sendet media_apply das Magic-Packet selbst (variabel
# pflegbar, ohne YAML-Hardcode). Leer = nur turn_on (Leuchtfeuer sendet das Packet).
CONF_TV_WOL_MAC: Final[str] = "tv_wol_mac"
DEFAULT_TV_WOL_MAC: Final[str] = ""

# Phase 3b — Sleep-TV-Off (R24). Sleep aktiv + TV läuft → nach delay Warnung auf
# dem TV, dann (nach warn_lead) TV aus, sofern nicht verlängert (Lichtschalter).
CONF_SLEEP_TV_OFF_DELAY: Final[str] = "sleep_tv_off_delay_seconds"
CONF_SLEEP_TV_WARN_LEAD: Final[str] = "sleep_tv_warn_lead_seconds"
CONF_SLEEP_TV_NOTIFY: Final[str] = "sleep_tv_notify_service"   # z.B. "notify.living_lgtv"
CONF_SLEEP_TV_WARN_MESSAGE: Final[str] = "sleep_tv_warn_message"
DEFAULT_SLEEP_TV_OFF_DELAY: Final[float] = 2700.0   # 45 min (Lastenheft R24)
DEFAULT_SLEEP_TV_OFF_CONFIRM: Final[float] = 600.0  # Issue #59: 10 min continuous
DEFAULT_SLEEP_TV_WARN_LEAD: Final[float] = 60.0     # Warnung 1 min vor Aus
DEFAULT_SLEEP_TV_NOTIFY: Final[str] = "notify.lg_webos_tv_oled77c47la"  # benni-TV (Legacy-notify-Service, kein _deuqdjp-Suffix); leer = keine Warnung
DEFAULT_SLEEP_TV_WARN_MESSAGE: Final[str] = (
    "Sleep-Modus wird in 1 Minute aktiv, TV wird ausgeschaltet."
)

# R23 — Wake-Sequenz. HomePods starten bei start_volume, nach debounce auf das
# media_policy-Ziel rampen. KH-3 (Wecker-Hook = separate Alarm-Lautstärke) ist
# als Schnittstelle vorgesehen, aber noch nicht implementiert; KH-4 (bio_state
# 'waking' wie 'awake') ist durch das „nicht sleep"-Gate automatisch erfüllt.
CONF_WAKE_START_VOLUME: Final[str] = "wake_start_volume"
CONF_WAKE_DEBOUNCE: Final[str] = "wake_debounce_seconds"
DEFAULT_WAKE_START_VOLUME: Final[float] = 0.10   # Lastenheft 20_helpers
DEFAULT_WAKE_DEBOUNCE: Final[float] = 5.0
# Race-Fix: bei Wake feuert der Radio-Autostart auf derselben Flanke wie die
# Wake-Sequenz. Der Volume-Floor (0.10) wird blockierend gesetzt; dieser kurze
# Vorlauf hält die Wiedergabe zurück, bis der Floor anliegt — sonst gibt der
# HomePod einen Sekundenbruchteil bei alter (lauter) Lautstärke aus.
CONF_WAKE_PLAY_LEAD: Final[str] = "wake_play_lead_seconds"
DEFAULT_WAKE_PLAY_LEAD: Final[float] = 1.0

RAMP_SETTING_DEFAULTS: Final[dict[str, Any]] = {
    CONF_RAMP_STEPS: DEFAULT_RAMP_STEPS,
    CONF_RAMP_STEP_DELAY: DEFAULT_RAMP_STEP_DELAY,
    CONF_TINY_DELTA: DEFAULT_TINY_DELTA,
    CONF_DUCKED_LEVEL: DEFAULT_DUCKED_LEVEL,
    CONF_DEBOUNCE_SECONDS: DEFAULT_DEBOUNCE_SECONDS,
    CONF_DEBOUNCE_MAX_WAIT: DEFAULT_DEBOUNCE_MAX_WAIT,
    CONF_DENON_IMMEDIATE: DEFAULT_DENON_IMMEDIATE,
}

# --------------------------------------------------------------------------- #
# Status-/Debug-Roster (Output-Entities). Im Shadow zeigt das den geplanten,
# nicht ausgeführten Apply-Plan.
# --------------------------------------------------------------------------- #
UID_LAST_ACTION: Final[str] = "last_action"
UID_HOMEPODS_TARGET: Final[str] = "homepods_target"
UID_DENON_TARGET: Final[str] = "denon_target"
UID_RAMP_ACTIVE: Final[str] = "ramp_active"
UID_APPLY_ENABLED: Final[str] = "apply_enabled"
UID_EXECUTE: Final[str] = "execute"
UID_NACHLAUF_ACTIVE: Final[str] = "denon_nachlauf_active"
UID_PLAYBACK_HEALTH: Final[str] = "playback_health"
UID_PLAYBACK_RECOVERY_STAGE: Final[str] = "playback_recovery_stage"
UID_SLEEP_TV_EVIDENCE: Final[str] = "sleep_tv_evidence"

DEFAULT_DATA: Final[dict[str, Any]] = {
    "last_action": ACTION_NONE,
    "homepods_target": None,
    "denon_target": None,
    "ramp_active": False,
    "apply_enabled": DEFAULT_APPLY_ENABLED,
    "execute": False,
    "denon_nachlauf_active": False,
    "playback_health": "idle",
    "playback_health_attrs": {},
    "playback_recovery_stage": "idle",
    "sleep_tv_evidence": "inactive",
    "sleep_tv_evidence_attrs": {},
}
