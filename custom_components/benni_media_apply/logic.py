"""HA-freie Apply-Engine für benni_media_apply (Executor).

Rechnet NICHTS neu — nimmt die Targets/Action aus media_policy und entscheidet,
WAS am Gerät zu tun ist: idempotent (nur bei Ist≠Soll) und geramped (HomePods
16×1s, Tiny-Delta direkt; Denon hart). Quiet → direkt (kein Ramp). Apply-Gate:
`apply_enabled` (global, Shadow) × `volume_apply_allowed` (pro Entscheidung).

Keine HA-Imports. Der Coordinator macht das Entity-State-Plumbing, führt die
Ramp-Sequenz als (abbrechbaren) Task aus und ruft die Services.

Phase 1 (FLEET-40): Volume (Ramp/direct), HomePods-Action (pause/play;
start_radio delegiert der Coordinator an ein Script), Subwoofer on/off.
Restore (R20), Denon-Nachlauf (R13/R14), Sleep-Off (R24/R25) folgen.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Final, Optional, Sequence

from .const import (
    ACTION_DENON_OFF,
    ACTION_NONE,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_START_RADIO,
    BIO_PROVISIONAL_SLEEP_VALUE,
    BIO_SLEEP_CONTEXT_VALUES,
    BIO_SLEEP_VALUE,
    DENON_CONSUMER_POWER_CHECKED,
    DEV_LABEL_PC,
    DEV_LABEL_TV,
    DEFAULT_DEBOUNCE_MAX_WAIT,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_DENON_IMMEDIATE,
    DEFAULT_DUCKED_LEVEL,
    DEFAULT_RADIO_DISPATCH_COOLDOWN,
    DEFAULT_RADIO_DISPATCH_MAX_BACKOFF,
    DEFAULT_SLEEP_TV_OFF_CONFIRM,
    DEFAULT_SLEEP_TV_OFF_DELAY,
    DEFAULT_RAMP_STEP_DELAY,
    DEFAULT_RAMP_STEPS,
    DEFAULT_TINY_DELTA,
    DEFAULT_WAKE_DEBOUNCE,
    DEFAULT_WAKE_START_VOLUME,
    EXEC_DEBOUNCE,
    EXEC_IMMEDIATE,
    EXEC_SHADOW,
    PLAYER_ADDRESSABLE_VALUES,
    PLAYER_OFF_VALUES,
    PLAYER_PLAYING_VALUES,
    RADIO_CATALOG,
    RADIO_STATION_LABELS,
    SCREEN_DEVICES,
)


# --------------------------------------------------------------------------- #
# Inputs / Settings / Plan
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Inputs:
    """Snapshot der Apply-Eingänge. None = unknown/nicht gebunden."""

    apply_enabled: bool = False           # globaler Shadow-Kill-Switch (Option)
    # aus media_policy:
    volume_apply_allowed: bool = False
    action: str = ACTION_NONE
    homepods_should_pause: bool = False
    homepods_resume_allowed: bool = False
    homepods_target: Optional[float] = None
    denon_target: Optional[float] = None
    subwoofer_allowed: bool = False
    # aus media_state:
    quiet_mode: bool = False
    presence_state: Optional[str] = None
    presence_degraded: bool = False
    away_gate: Optional[bool] = None
    stop_latch: bool = False
    # Radio (Phase 4b). None = ungebunden/unbekannt ⇒ non-regressiv (erlauben).
    radio_station: Optional[str] = None
    radio_ready: Optional[bool] = None
    manual_playback: Optional[bool] = None
    planned_station_playing: Optional[bool] = None   # FLEET-79 Autostart-Gate
    # aktueller Geräte-Zustand (Ist, für Idempotenz):
    homepods_configured: bool = False
    homepods_state: Optional[str] = None
    homepods_volume: Optional[float] = None
    denon_configured: bool = False
    denon_state: Optional[str] = None
    denon_volume: Optional[float] = None
    subwoofer_configured: bool = False
    subwoofer_state: Optional[str] = None   # "on"/"off"/None
    # Phase 3 (R13/R14 Denon-Nachlauf). None = unbekannt/nicht gebunden ⇒ kein Arm.
    pc_power_on: Optional[bool] = None
    tv_power_on: Optional[bool] = None
    denon_power_on: Optional[bool] = None
    bio_sleep: Optional[bool] = None
    bio_state: Optional[str] = None
    sleep_source: Optional[str] = None
    sleep_reference_start: Optional[str] = None
    # FLEET-80 — Cross-Source-Gate: ist ein ANDERER Denon-Konsument (TV/ATV/PS5/
    # Switch/PC) aktiv? Dann darf der Nachlauf den geteilten Denon NICHT
    # ausschalten. None = unbekannt ⇒ konservativ wie „aktiv" (Denon bleibt an).
    denon_consumer_active: Optional[bool] = None
    # Phase 4c (R12 TV-WoL). media_device = aktives Output-Gerät (media_state);
    # tv_player_state = WebOS-State (R11 primär). None = ungebunden/unbekannt.
    media_device: Optional[str] = None
    tv_player_state: Optional[str] = None
    # Phase 3b (R24 Sleep-TV-Off). Flanke vom Coordinator (Lichtschalter-Druck).
    sleep_tv_extend_pressed: bool = False
    # R23 (Wake-Sequenz). Flanke vom Coordinator (ein Wake-Trigger ging an).
    wake_trigger_fired: bool = False
    # control#3: Private Time aktiv (audio_owner == private_stack). Steuert den
    # Private-Exit-Denon-Off-Delay + die Wake-Sperre. None = ungebunden/unbekannt.
    private_active: Optional[bool] = None
    # benni_media#16: roher audio_owner (media_policy). Ein konkurrierender Owner
    # (private_stack/tv_denon/…) sperrt den (verzögerten) Radio-Autostart, nicht
    # nur der TV. None/unbekannt = non-regressiv (kein Block).
    audio_owner: Optional[str] = None
    # control#3: HomePod-Start sperren, solange der Private-Exit-Delay laeuft und
    # der Denon noch an ist (kein kurzer Parallelbetrieb). Coordinator setzt es.
    suppress_homepods_start: bool = False


@dataclass(frozen=True)
class RampSettings:
    ramp_steps: int = DEFAULT_RAMP_STEPS
    ramp_step_delay_s: float = DEFAULT_RAMP_STEP_DELAY
    tiny_delta: float = DEFAULT_TINY_DELTA
    ducked_level: float = DEFAULT_DUCKED_LEVEL
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS  # R2-Fenster (Coordinator-Timing)
    # benni_media#13: Deckel fürs Neu-Anstoßen des R2-Fensters (Anti-Starvation).
    debounce_max_wait_s: float = DEFAULT_DEBOUNCE_MAX_WAIT
    # benni_media#13: Denon-Volume am Debounce vorbei (der AVR kann keine Rampe).
    denon_immediate: bool = DEFAULT_DENON_IMMEDIATE
    wake_start_volume: float = DEFAULT_WAKE_START_VOLUME  # R23 HomePods-Startlautstärke
    wake_debounce_seconds: float = DEFAULT_WAKE_DEBOUNCE


@dataclass
class ApplyState:
    """Persistenter Zustand zwischen Coordinator-Ticks (RAM). Trägt den
    R20-Pre-Quiet-Snapshot + die Quiet-Edge-Buchführung."""

    was_quiet: bool = False
    pre_quiet_homepods: Optional[float] = None   # Pre-Quiet-Target (Snapshot, R20)
    pre_quiet_denon: Optional[float] = None
    last_homepods_target: Optional[float] = None  # Vortick-Target (Quelle des Snapshots)
    last_denon_target: Optional[float] = None
    # Zuletzt an den Denon GESETZTER Pegel. Idempotenz-Anker für den watt-primären
    # Pfad (Player meldet stale "off" ⇒ Ist-Volume nicht lesbar): verhindert, dass
    # bei jedem Watt-Report derselbe Pegel neu geschrieben wird (Volume-OSD-Flackern).
    applied_denon: Optional[float] = None


@dataclass(frozen=True)
class RadioDispatchState:
    """In-memory admission/backoff state for automatic radio starts.

    The state intentionally belongs to the executor, not to the media policy:
    it protects the external dispatch side effect and does not change the
    policy's desired action. A Home Assistant restart resets it safely.
    """

    next_allowed_at: float = 0.0
    consecutive_failures: int = 0
    last_attempt_at: Optional[float] = None
    last_source: Optional[str] = None
    last_error: Optional[str] = None


@dataclass(frozen=True)
class PlaybackHealth:
    """Observable health of the wake playback path.

    This deliberately evaluates only signals Home Assistant can prove. It can
    detect a non-playing group, a non-playing/unavailable member and an explicit
    mute flag. It cannot prove acoustic output when every player reports healthy.
    """

    state: str
    reason: Optional[str] = None


@dataclass(frozen=True)
class UnmuteResult:
    """Result of one bounded, state-confirmed unmute operation."""

    state: str
    attempts: int
    remaining: tuple[str, ...] = ()
    reason: Optional[str] = None


@dataclass
class ApplyPlan:
    """Was der Coordinator tun soll. Im Shadow (execute=False) nur Debug."""

    execute: bool = False                  # apply_enabled (globaler Gate)
    homepods_action: str = ACTION_NONE     # pause/play/start_radio/none
    denon_action: str = ACTION_NONE        # currently: turn_off_denon
    homepods_levels: list = field(default_factory=list)  # Volume-Set-Sequenz
    homepods_ramp: bool = False            # True = gestuft (Ramp-Task), False = direkt
    denon_set: Optional[float] = None      # harter Set-Wert (None = no-op)
    subwoofer_set: Optional[bool] = None   # True/False/None (None = no-op)
    away_block: bool = False
    quiet_override: bool = False           # Quiet → direkt, laufenden Ramp abbrechen
    is_restore: bool = False               # R20: Quiet-Ende → Ramp-Up auf Pre-Quiet
    radio_uri: Optional[str] = None        # aufgelöster Sender-URI (start_radio inline)
    reasons: list = field(default_factory=list)

    @property
    def has_work(self) -> bool:
        """True, wenn der Plan tatsächlich etwas am Gerät tut. Triviale Pläne
        (nur Re-Eval ohne Soll≠Ist) dürfen ein laufendes Debounce-Fenster NICHT
        neu starten — sonst hungert ein gepufferter echter Plan aus."""
        return bool(
            self.homepods_action != ACTION_NONE
            or self.denon_action != ACTION_NONE
            or self.homepods_levels
            or self.denon_set is not None
            or self.subwoofer_set is not None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "execute": self.execute,
            "homepods_action": self.homepods_action,
            "denon_action": self.denon_action,
            "homepods_target": self.homepods_levels[-1] if self.homepods_levels else None,
            "homepods_ramp": self.homepods_ramp,
            "denon_target": self.denon_set,
            "subwoofer_set": self.subwoofer_set,
            "away_block": self.away_block,
            "quiet_override": self.quiet_override,
            "is_restore": self.is_restore,
            "radio_uri": self.radio_uri,
            "reasons": list(self.reasons),
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _eq(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


def ramp_levels(
    current: Optional[float], target: Optional[float], steps: int, tiny_delta: float
) -> list[float]:
    """Volume-Set-Sequenz von current → target.

    - target None         → [] (nichts zu tun)
    - current None        → [target] (kein Ist → direkt setzen)
    - |Δ| == 0            → [] (Ist == Soll, idempotenter No-op)
    - |Δ| <= tiny_delta   → [target] (Tiny-Delta → direkt, kein Ramp)
    - sonst               → `steps` Zwischenstufen, letzte == target
    """
    if target is None:
        return []
    t = round(_clamp(target, 0.0, 1.0), 3)
    if current is None:
        return [t]
    c = round(_clamp(current, 0.0, 1.0), 3)
    delta = t - c
    if _eq(delta, 0.0):
        return []
    if abs(delta) <= tiny_delta:
        return [t]
    n = max(1, int(steps))
    return [round(c + delta * i / n, 3) for i in range(1, n + 1)]


def resolve_radio_uri(station: Optional[str]) -> Optional[str]:
    """Sender-Key → radiobrowser-URI (Phase 4b Katalog-Port). None bei
    ungebundenem/unbekanntem Sender ⇒ Coordinator fällt auf das YAML-Script zurück."""
    if not station:
        return None
    return RADIO_CATALOG.get(station)


def screen_blocks_music_start(inp: "Inputs") -> bool:
    """benni_media#16 — Besitzt gerade ein Bildschirm-Stack (TV) das Audio, sodass
    kein Musikstart erfolgen darf?

    Belegter Live-Folgefehler (2026-08-01, TV-Start ~22:17): Der TV-Master
    flackerte beim Hochfahren extern (active→off→active, Watt-Dip auf 43 W). Im
    kurzen Falsch-Musik-Fenster endete die manuelle Wiedergabe (`manual_playback`
    on→off, 22:17:54) und armte den verzögerten Radio-Resume (Trigger B,
    `RADIO_RESUME_DELAY` 10 s). Als er 22:18:04 feuerte, war der TV längst stabil
    aktiv (`audio_owner=tv_denon`, `homepods_resume_allowed=off`), aber weder
    `should_autostart_radio` (TV-blind) noch `action==PAUSE` (Gruppe schon idle →
    `action=none`) fingen das ab → GAY.FM startete unter laufendem TV und musste
    erneut pausiert werden.

    Regel: Ist der TV zum Ausführungszeitpunkt an, wird kein (verzögerter)
    Musikstart ausgelöst. Geprüft wird der STABILE Ist-Zustand (WebOS primär,
    Watt-Fallback über `_tv_is_off`), nicht ein veraltetes wartendes Startsignal —
    damit robust gegen den kurzen TV-Master-Flacker. ``None`` (TV unbekannt) blockt
    NICHT (non-regressiv).

    benni_media#16 — Generalisierung auf den Audio-Owner (nicht nur TV): belegt
    03.08. 01:28 startete ein verzögerter Radio-Resume (Trigger B, durch die
    manual-off-Flanke beim Pausieren für private_time gearmt) GAY.FM MITTEN im
    private_time, weil dieser Guard nur den TV prüfte (TV war aus →
    ``audio_owner=private_stack``, aber nicht geblockt) → Flap. Jetzt blockt
    zusätzlich JEDER konkurrierende Owner: alles außer ``homepods``/``none`` (und
    unbekannt/unbound → non-regressiv). Deckt private_stack, tv_denon und künftige
    Konsumenten (ps5/pc) ab."""
    if _tv_is_off(inp) is False:
        return True
    owner = (inp.audio_owner or "").strip().lower()
    return owner not in ("", "unknown", "unavailable", "homepods", "none")


def radio_dispatch_admit(
    state: RadioDispatchState,
    now: float,
    *,
    source: str,
    automatic: bool = True,
    cooldown_s: float = DEFAULT_RADIO_DISPATCH_COOLDOWN,
) -> tuple[bool, RadioDispatchState, str]:
    """Reserve one automatic radio dispatch, or reject it during cooldown.

    Admission is synchronous/pure so concurrent coordinator tasks cannot both
    pass the guard before either external service call starts. Manual playback
    is explicitly outside the guard and does not mutate the state.
    """
    if not automatic:
        return True, state, "manual"
    now = float(now)
    if now < state.next_allowed_at:
        return False, state, "cooldown"
    delay = max(0.0, float(cooldown_s))
    return (
        True,
        replace(
            state,
            next_allowed_at=now + delay,
            last_attempt_at=now,
            last_source=source,
            last_error=None,
        ),
        "allowed",
    )


def radio_dispatch_result(
    state: RadioDispatchState,
    now: float,
    *,
    success: bool,
    error: Optional[str] = None,
    cooldown_s: float = DEFAULT_RADIO_DISPATCH_COOLDOWN,
    max_backoff_s: float = DEFAULT_RADIO_DISPATCH_MAX_BACKOFF,
) -> RadioDispatchState:
    """Record the result and keep a failed provider on exponential backoff."""
    now = float(now)
    base = max(0.0, float(cooldown_s))
    if success:
        return replace(
            state,
            next_allowed_at=max(state.next_allowed_at, now + base),
            consecutive_failures=0,
            last_error=None,
        )

    failures = state.consecutive_failures + 1
    backoff = min(
        max(base, float(max_backoff_s)),
        base * (2 ** max(0, failures - 1)),
    )
    return replace(
        state,
        next_allowed_at=max(state.next_allowed_at, now + backoff),
        consecutive_failures=failures,
        last_error=str(error or "radio_dispatch_failed"),
    )


def radio_dispatch_remaining(state: RadioDispatchState, now: float) -> float:
    """Return the remaining automatic cooldown in seconds."""
    return round(max(0.0, state.next_allowed_at - float(now)), 2)


def should_autostart_radio(inp: "Inputs") -> bool:
    """FLEET-79: Gate für den Radio-Autostart (Wake / Resume). Nur wenn ein gültiger
    Sender bereit ist (`radio_ready` True), KEINE manuelle Wiedergabe läuft und die
    geplante Station NICHT eh schon spielt. Der Trigger (Wake-Flanke / manual-off-
    Flanke) sowie das Latch-Lösen liegen im Coordinator. None (ungebunden) = blockt
    (radio_ready muss explizit True sein → kein Autostart ohne validen Sender).
    Während `bio_sleep` bleiben automatische Starts und Resumes gesperrt (#45).
    benni_media#16: bei aktivem TV wird nie Musik gestartet (screen_blocks_music_start)."""
    return (
        media_block_reason(inp) is None
        and not presence_holds(inp)
        and inp.bio_sleep is not True
        and inp.radio_ready is True
        and inp.manual_playback is not True
        and inp.planned_station_playing is not True
        and not screen_blocks_music_start(inp)
    )


def playback_start_block_reason(
    inp: "Inputs", *, require_positive_target: bool = True
) -> Optional[str]:
    """Return why a delayed Wake start must stop before touching playback.

    Every delayed stage reuses this pure gate. That prevents a soft retry or an
    app restart after the user went back to sleep, pressed Stop, started manual
    playback or handed audio ownership to a screen/private stack.
    """
    if not inp.apply_enabled:
        return "apply_disabled"
    blocked = media_block_reason(inp)
    if blocked:
        return blocked
    if presence_holds(inp):
        return "presence_unknown"
    if inp.bio_sleep is True:
        return "bio_sleep"
    if inp.stop_latch:
        return "stop_latch"
    if inp.action == ACTION_PAUSE:
        return "policy_pause"
    if not inp.homepods_resume_allowed:
        return "resume_not_allowed"
    if inp.suppress_homepods_start:
        return "start_suppressed"
    if inp.radio_ready is not True:
        return "radio_not_ready"
    if inp.manual_playback is True:
        return "manual_playback"
    if screen_blocks_music_start(inp):
        return "competing_audio_owner"
    if require_positive_target and (
        inp.homepods_target is None or inp.homepods_target <= 0.0
    ):
        return "non_positive_target"
    return None


def playback_recovery_block_reason(
    inp: "Inputs", *, require_positive_target: bool = True
) -> Optional[str]:
    """Backward-compatible name for the pre-start Wake admission gate."""

    return playback_start_block_reason(
        inp, require_positive_target=require_positive_target
    )


def playback_repair_block_reason(
    inp: "Inputs",
    *,
    managed_episode: bool,
    require_positive_target: bool = True,
) -> Optional[str]:
    """Fail-closed gate for unmute/health work after a managed start.

    ``homepods_resume_allowed`` is intentionally absent: it is a transient start
    permission and normally falls back to false once playback is running. Repair
    instead needs durable evidence for a managed episode and every current safety
    condition. This also prevents a future intentional mute/ducking path from
    being undone without an explicit contract.
    """

    if not managed_episode:
        return "ownership_unproven"
    if not inp.apply_enabled:
        return "apply_disabled"
    blocked = media_block_reason(inp)
    if blocked:
        return blocked
    if presence_holds(inp):
        return "presence_unknown"
    if inp.bio_sleep is True or inp.bio_state in BIO_SLEEP_CONTEXT_VALUES:
        return "sleep_context"
    if inp.bio_state not in ("waking", "awake"):
        return "bio_state_unproven"
    if inp.stop_latch:
        return "stop_latch"
    if inp.action == ACTION_PAUSE or inp.homepods_should_pause:
        return "policy_pause"
    if not inp.volume_apply_allowed:
        return "volume_apply_not_allowed"
    if inp.quiet_mode:
        return "intentional_ducking"
    if inp.suppress_homepods_start:
        return "start_suppressed"
    if inp.radio_ready is not True:
        return "radio_not_ready"
    if inp.manual_playback is not False:
        return "manual_playback" if inp.manual_playback else "manual_playback_unproven"
    owner = (inp.audio_owner or "").strip().lower()
    if owner != "homepods":
        return (
            "competing_audio_owner"
            if owner not in ("", "unknown", "unavailable", "none")
            else "audio_owner_unproven"
        )
    if require_positive_target and (
        inp.homepods_target is None or inp.homepods_target <= 0.0
    ):
        return "non_positive_target"
    return None


def stuck_mute_targets(
    *,
    group_state: Optional[str],
    entity_ids: Sequence[str],
    pod_states: Sequence[Optional[str]],
    pod_muted: Sequence[Optional[bool]],
) -> tuple[str, ...]:
    """Return only configured, playing members with an evidenced mute flag."""

    if group_state not in PLAYER_PLAYING_VALUES:
        return ()
    return tuple(
        entity_id
        for entity_id, state, muted in zip(entity_ids, pod_states, pod_muted)
        if state in PLAYER_PLAYING_VALUES and muted is True
    )


async def bounded_unmute(
    targets: Sequence[str],
    *,
    unmute: Callable[[tuple[str, ...]], Awaitable[None]],
    remaining_muted: Callable[[], tuple[str, ...]],
    wait: Callable[[float], Awaitable[None]],
    block_reason: Callable[[], Optional[str]],
    max_attempts: int = 2,
    retry_delay: float = 2.0,
) -> UnmuteResult:
    """Unmute, re-read actual state and retry a small bounded number of times."""

    remaining = tuple(dict.fromkeys(targets))
    if not remaining:
        return UnmuteResult("not_needed", 0)
    attempts = 0
    for _ in range(max(1, int(max_attempts))):
        reason = block_reason()
        if reason is not None:
            return UnmuteResult("cancelled", attempts, remaining, reason)
        attempts += 1
        try:
            await unmute(remaining)
        except Exception as err:  # noqa: BLE001 - returned as bounded diagnostics.
            if attempts >= max(1, int(max_attempts)):
                return UnmuteResult(
                    "failed", attempts, remaining, f"unmute_service_failed:{err}"
                )
        await wait(max(0.0, float(retry_delay)))
        remaining = tuple(dict.fromkeys(remaining_muted()))
        if not remaining:
            return UnmuteResult("recovered", attempts)
    return UnmuteResult("failed", attempts, remaining, "mute_state_stuck")


def playback_health(
    *,
    group_state: Optional[str],
    pod_states: list[Optional[str]],
    pod_muted: list[Optional[bool]],
    target: Optional[float],
) -> PlaybackHealth:
    """Classify the signals that identify the observed AirPlay hang (#41)."""
    if target is None or target <= 0.0:
        return PlaybackHealth("inactive", "non_positive_target")
    if group_state not in PLAYER_PLAYING_VALUES:
        return PlaybackHealth("unhealthy", f"group_{group_state or 'unavailable'}")
    for index, state in enumerate(pod_states):
        if state not in PLAYER_PLAYING_VALUES:
            return PlaybackHealth(
                "unhealthy", f"pod_{index + 1}_{state or 'unavailable'}"
            )
    for index, muted in enumerate(pod_muted):
        if muted is True:
            return PlaybackHealth("unhealthy", f"pod_{index + 1}_muted")
        if muted is not False:
            return PlaybackHealth("unhealthy", f"pod_{index + 1}_mute_unknown")
    return PlaybackHealth("healthy")


def suppress_parallel_wake_start(plan: "ApplyPlan", wake_owned: bool) -> "ApplyPlan":
    """Keep volume work but remove competing start commands during a wake episode."""
    if not wake_owned or plan.homepods_action not in (ACTION_RESUME, ACTION_START_RADIO):
        return plan
    return replace(
        plan,
        homepods_action=ACTION_NONE,
        reasons=[*plan.reasons, "wake:single_flight_owner"],
    )


def wake_ramp_target(
    inp: "Inputs", start_volume: float, target: Optional[float]
) -> Optional[float]:
    """Hold the wake floor when an eligible radio target transiently becomes 0.

    A valid zero target remains valid outside an automatic-radio wake. During a
    wake where an automatic start is still eligible, zero is commonly the
    transient idle target while the external radio provider is failing; ramping
    down to it would erase the wake floor before the next policy tick converges.
    """
    if target is None:
        return None
    if target <= 0.0 and should_autostart_radio(inp):
        return max(0.0, min(1.0, float(start_volume)))
    return target


def media_block_reason(inp: Inputs) -> Optional[str]:
    """Höchstpriorer Abwesenheits-Block für automatische Apply-Pfade. NUR echte
    Abwesenheit (away) pausiert/gatet hart. `unknown`/degraded ist BEWUSST kein
    Block: media_state flappt beim HA-Neustart kurz `presence_state=unknown`, und
    das darf laufende Musik NICHT pausieren (war die Wurzel des Restart-Stopps —
    Apply setzte away_block:presence_unknown → pause_homepods). Mirror der
    media_policy v0.13.1: unknown hält nur den Auto-Start zurück (presence_holds),
    fasst aber laufende Musik nicht an."""
    if inp.away_gate is True:
        return "away_gate"
    presence = (inp.presence_state or "").strip().lower()
    if presence == "abwesend":
        return "presence_away"
    return None


def presence_holds(inp: "Inputs") -> bool:
    """unknown/degraded Presence: kein Radio-Auto-Start (wir wissen nicht, ob
    zuhause), ABER kein Pause/Away-Block — laufende Musik bleibt unberührt."""
    presence = (inp.presence_state or "").strip().lower()
    return presence == "unknown" or inp.presence_degraded is True


def radio_defaults() -> list[dict[str, str]]:
    """Default-Sender als Shortcut-Liste fürs Cockpit: [{key, name, uri}].
    Name aus RADIO_STATION_LABELS (Fallback: Key), sortiert nach Anzeigenamen."""
    out = [
        {"key": key, "name": RADIO_STATION_LABELS.get(key, key), "uri": uri}
        for key, uri in RADIO_CATALOG.items()
    ]
    return sorted(out, key=lambda s: s["name"].lower())


def _direct(current: Optional[float], target: Optional[float]) -> list[float]:
    """Einzelner, idempotenter Direkt-Set (kein Ramp). [] wenn Ist==Soll."""
    if target is None:
        return []
    t = round(_clamp(target, 0.0, 1.0), 3)
    if current is None:
        return [t]
    if _eq(t, round(_clamp(current, 0.0, 1.0), 3)):
        return []
    return [t]


def homepods_volume_addressable(action: str, hp_state: Optional[str]) -> bool:
    """benni_media#16 — Darf die HomePods-Gruppe jetzt einen Volume-Befehl bekommen?

    Ein AirPlay-/Music-Assistant-Gruppen-Player nimmt ``volume_set`` NICHT neutral
    entgegen: Auf einem pausierten bzw. ``idle`` Player **weckt** ein ``volume_set``
    die Wiedergabe wieder auf, und hörbar ist der Pegel dort ohnehin nicht.

    Belegte Live-Evidenz (Recorder, 2026-08-01, Musik→TV): Nach der Pause
    (``15:28:22`` Gruppe ``idle``) lief die Volume-Rampe gegen ``0.0`` auf dem
    pausierten Gruppen-Player WEITER (0.42 → 0.39 → … → 0.01, 1-s-Takt). Genau bei
    zwei dieser ``volume_set``-Schritte sprang die Gruppe zurück auf ``playing``
    (``15:28:39`` bei 0.12, ``15:28:45`` bei 0.07); die Policy re-pausierte jedes
    Mal → ``pause_homepods`` feuerte 3× und die Gruppe flappte, obwohl der
    TV-Kontext stabil war. Im System-Log scheiterten dieselben ``volume_set
    volume=0.0``-Calls zusätzlich.

    Regel: Volume nur, wenn wir die Gruppe gerade STARTEN (``resume``/``start_radio``
    — das Setzen der Start-/Wake-Lautstärke gehört zum Start) ODER sie bereits
    ``playing`` ist. Beim ``pause`` nie — die Pause ist der Stop-Mechanismus, nicht
    ``volume 0`` (Kernsemantik aus #16). Die 16×1-s-Wake/Resume-Rampe bleibt davon
    unberührt (sie läuft über ``resume``/``start_radio``).
    """
    if action == ACTION_PAUSE:
        return False
    if action == ACTION_START_RADIO:
        return True
    return hp_state in PLAYER_PLAYING_VALUES


def volume_target_entities(pods: Any, group: Optional[str]) -> list[str]:
    """benni_media#16 — Ziel-Entities für HomePods-Volume/Ramp.

    Lastenheft „pro Gerät einzeln (kein Gruppen-Call)": ein `volume_set` auf die
    AirPlay-Sync-GRUPPE weckt einen pausierten Verbund wieder auf (belegter Live-
    Bug), auf die einzelnen Pods nicht. Ist die Pod-Liste gebunden, wird sie
    genutzt; sonst Fallback auf die Gruppe (non-regressiv). Pause/Resume/Radio
    adressieren weiterhin die Gruppe — nur Volume geht pro Pod.
    """
    if isinstance(pods, (list, tuple)):
        ids = [e for e in pods if isinstance(e, str) and e]
        if ids:
            return ids
    return [group] if isinstance(group, str) and group else []


# --------------------------------------------------------------------------- #
# Ausführungs-Modus (R2 Debounce / R3 Queue-statt-Race)
# --------------------------------------------------------------------------- #
def execution_mode(plan: "ApplyPlan") -> str:
    """Entscheidet, WIE der berechnete Plan zum Gerät kommt (Pure-Teil von R2/R3).

    - ``EXEC_SHADOW``: ``apply_enabled`` aus → gar nicht ausführen (nur Preview).
    - ``EXEC_IMMEDIATE``: Quiet-Mode bricht sofort durch — kein Debounce, der
      laufende Ramp wird abgebrochen (R2/R3-Ausnahme). Gilt SYMMETRISCH auch für
      den R20-Restore (``is_restore``, Quiet-Ende): das Un-Ducking muss so prompt
      kommen wie das Ducking — sonst hängt der Pegel nach Tür-zu noch das volle
      Debounce-Fenster auf ducked_target (FLEET-81). Der Restore setzt
      ``quiet_override`` NICHT (sonst bräche `_execute` den Restore-Ramp ab), darum
      hier die explizite is_restore-Ausnahme.
    - ``EXEC_DEBOUNCE``: Normalfall — Ausführung wartet das R2-Fenster ab, sodass
      ein Trigger-Burst zu EINER konsolidierten Aktion zusammenfällt.

    Das reale Timing/Serialisieren liegt im Coordinator; hier wohnt nur die
    HA-freie Klassifikation (testbar)."""
    if not plan.execute:
        return EXEC_SHADOW
    if plan.quiet_override or plan.is_restore or plan.away_block:
        return EXEC_IMMEDIATE
    return EXEC_DEBOUNCE


def take_immediate_denon(plan: "ApplyPlan", enabled: bool = True) -> Optional[float]:
    """Entnimmt dem Plan den harten Denon-Volume-Set zur SOFORT-Ausführung.

    benni_media#13 — Geräte-differenziert, NICHT global:

    - **HomePods** können eine weiche Rampe fahren; die ist ausdrücklich gewollt
      (sanftes Ein-/Ausblenden, R23-Wake) und bleibt vollständig unangetastet:
      Rampe UND R2-Debounce gelten für die HomePods unverändert weiter.
    - **Der Denon** kann technisch keine sinnvolle Rampe abbilden — er bekommt
      ohnehin einen einzelnen harten `volume_set`. Für ihn ist das Debounce-
      Fenster reine Verzögerung: in der Evidenz lag das gültige Ziel 27 % um
      ``22:23:43.872`` an, der AVR stand aber erst ``22:23:49.176`` darauf
      (**+5.3 s**), obwohl nur EIN Service-Call nötig war.

    Sobald Zielkontext und Zielwert feststehen, geht der Denon-Set deshalb sofort
    raus. Mutiert den Plan (setzt ``denon_set`` auf None), damit der verbleibende
    Rest normal gepuffert wird und der Wert nach dem Fenster nicht ein zweites
    Mal geschrieben wird (AVR-OSD-Flackern). Der Idempotenz-Anker
    ``ApplyState.applied_denon`` wurde von `decide_apply` bereits fortgeschrieben.

    ``enabled=False`` → altes Verhalten (Denon läuft mit durchs Fenster).
    """
    if not enabled:
        return None
    value = plan.denon_set
    plan.denon_set = None
    return value


def debounce_decision(
    plan: "ApplyPlan",
    window_active: bool,
    window_age_s: Optional[float] = None,
    max_wait_s: Optional[float] = None,
) -> tuple[bool, bool]:
    """R2/R3 Pending-Buchführung für den EXEC_DEBOUNCE-Fall (pure). Return
    ``(update_pending, restart_window)``.

    - Echter Plan (``has_work``) → puffern UND das Fenster (neu) anstoßen.
    - No-Op-Plan bei LAUFENDEM Fenster → gepufferten Plan trotzdem auf diesen
      Stand bringen, damit eine inzwischen ÜBERHOLTE Aktion nicht doch noch
      ausgeführt wird (FLEET-245 Grind-Race: context→gaming puffert pause_homepods,
      3 ms später hebt subcontext→grind sie auf, aber der No-Op-Plan konnte den
      stale pause bisher nicht canceln). Fenster NICHT neu anstoßen → Anti-
      Starvation bleibt, latest-wins gilt jetzt auch fürs Zurücknehmen.
    - No-Op-Plan ohne laufendes Fenster → nichts tun.

    benni_media#13 — Anti-Starvation-Deckel: Bisher stieß JEDER Plan mit Arbeit
    das Fenster neu an. Ein Szenario-Übergang ist aber genau ein Trigger-BURST,
    jeder Schritt ein neuer Plan mit Arbeit — die Ausführung wurde dadurch immer
    weiter nach hinten geschoben, statt nach dem Fenster einmal konsolidiert zu
    laufen. Ist das laufende Fenster älter als ``max_wait_s``, wird weiter
    gepuffert (latest-wins bleibt), das Fenster aber NICHT mehr verlängert. Ohne
    ``window_age_s``/``max_wait_s`` (Alt-Aufrufer, Tests) gilt das Verhalten
    unverändert.
    """
    if plan.has_work:
        starved = (
            window_active
            and window_age_s is not None
            and max_wait_s is not None
            and window_age_s >= max_wait_s
        )
        return True, not starved
    if window_active:
        return True, False
    return False, False


# --------------------------------------------------------------------------- #
# Master-Entscheidung
# --------------------------------------------------------------------------- #
def decide_apply(
    inp: Inputs,
    state: Optional[ApplyState] = None,
    settings: Optional[RampSettings] = None,
) -> tuple[ApplyPlan, ApplyState]:
    """Berechnet (Apply-Plan, nächster Zustand). Seiteneffekt-frei; der
    Coordinator führt aus + hält den ApplyState über die Ticks."""
    if settings is None:
        settings = RampSettings()
    if state is None:
        state = ApplyState()
    p = ApplyPlan()
    p.execute = inp.apply_enabled
    reasons: list[str] = []

    # ----- Quiet-Edges + Pre-Quiet-Snapshot (R20) -----
    quiet_entry = inp.quiet_mode and not state.was_quiet
    quiet_exit = (not inp.quiet_mode) and state.was_quiet
    new_state = ApplyState(
        was_quiet=inp.quiet_mode,
        pre_quiet_homepods=state.pre_quiet_homepods,
        pre_quiet_denon=state.pre_quiet_denon,
        # Vortick-Target nur außerhalb von Quiet fortschreiben — während Quiet
        # bleibt der Pre-Quiet-Wert eingefroren (sonst ginge er auf 0.10 verloren).
        last_homepods_target=inp.homepods_target if not inp.quiet_mode else state.last_homepods_target,
        last_denon_target=inp.denon_target if not inp.quiet_mode else state.last_denon_target,
        applied_denon=state.applied_denon,
    )
    if quiet_entry:
        # Snapshot des Pre-Quiet-Targets (der Vortick-Wert, vor dem Ducking).
        new_state.pre_quiet_homepods = state.last_homepods_target
        new_state.pre_quiet_denon = state.last_denon_target

    block_reason = media_block_reason(inp)
    if block_reason:
        p.away_block = True
        reasons.append(f"away_gate:{block_reason}")
        if inp.homepods_configured and inp.homepods_state in PLAYER_PLAYING_VALUES:
            p.homepods_action = ACTION_PAUSE
            reasons.append("action:pause_away")
        denon_on = (
            inp.denon_power_on is True
            or (inp.denon_state is not None and inp.denon_state not in PLAYER_OFF_VALUES)
        )
        if inp.denon_configured and denon_on:
            p.denon_action = ACTION_DENON_OFF
            reasons.append("action:denon_off_away")
        if inp.subwoofer_configured and inp.subwoofer_state == "on":
            p.subwoofer_set = False
            reasons.append("subwoofer:off_away")
        if not p.execute:
            reasons.append("shadow:apply_disabled")
        p.reasons = reasons
        return p, new_state

    # ----- HomePods-Action (geräte-zustands-idempotent) -----
    hp_playing = inp.homepods_state in PLAYER_PLAYING_VALUES
    action = inp.action or ACTION_NONE
    if action == ACTION_PAUSE and inp.homepods_should_pause and hp_playing:
        p.homepods_action = ACTION_PAUSE
        reasons.append("action:pause")
    elif (
        action == ACTION_RESUME
        and inp.homepods_resume_allowed
        and not hp_playing
        and not inp.stop_latch
        # control#3: waehrend des Private-Exit-Denon-Off-Delays keinen HP-Start.
        and not inp.suppress_homepods_start
    ):
        p.homepods_action = ACTION_RESUME
        reasons.append("action:resume")
    elif (
        action == ACTION_START_RADIO
        and not hp_playing
        and not inp.stop_latch
        and not inp.suppress_homepods_start
        # Radio-Gates wie im YAML-Script (None = ungebunden ⇒ non-regressiv erlauben).
        and inp.radio_ready is not False
        and inp.manual_playback is not True
    ):
        p.homepods_action = ACTION_START_RADIO
        p.radio_uri = resolve_radio_uri(inp.radio_station)
        reasons.append("action:start_radio")
    elif (
        action in (ACTION_RESUME, ACTION_START_RADIO)
        and inp.suppress_homepods_start
        and not hp_playing
    ):
        p.homepods_action = ACTION_NONE
        reasons.append("suppress:private_exit_denon")
    else:
        p.homepods_action = ACTION_NONE

    # benni_media#16 — Volume-Befehle nur an eine spielende bzw. gerade
    # gestartete HomePods-Gruppe, nie an eine pausierte/idle (das weckt den
    # AirPlay-Player und ist unhörbar). Gilt für Restore- UND Normalfall.
    hp_volume_ok = homepods_volume_addressable(p.homepods_action, inp.homepods_state)

    # ----- Volume (nur wenn die Policy es erlaubt) -----
    if inp.volume_apply_allowed:
        p.quiet_override = inp.quiet_mode
        if quiet_exit and new_state.pre_quiet_homepods is not None:
            # R20: Quiet-Ende → Restore auf Pre-Quiet (HomePods rampen, Denon hart).
            if inp.homepods_configured and hp_volume_ok:
                p.homepods_levels = ramp_levels(
                    inp.homepods_volume, new_state.pre_quiet_homepods,
                    settings.ramp_steps, settings.tiny_delta,
                )
                p.homepods_ramp = len(p.homepods_levels) > 1
                if p.homepods_levels:
                    p.is_restore = True
                    reasons.append("restore:r20_quiet_end")
            if (
                inp.denon_configured
                and new_state.pre_quiet_denon is not None
                and (
                    inp.denon_state in PLAYER_ADDRESSABLE_VALUES
                    or inp.denon_power_on is True
                )
            ):
                d = _direct(inp.denon_volume, new_state.pre_quiet_denon)
                p.denon_set = d[0] if d else None
                if p.denon_set is not None:
                    p.is_restore = True
                    reasons.append("restore:denon_hard")
        else:
            # ---- Phase-1-Normalfall ----
            if (
                inp.homepods_configured
                and inp.homepods_target is not None
                and hp_volume_ok
            ):
                if inp.quiet_mode:
                    # R20: Quiet → hart/direkt (kein Ramp), laufenden Ramp abbrechen.
                    p.homepods_levels = _direct(inp.homepods_volume, inp.homepods_target)
                    p.homepods_ramp = False
                else:
                    p.homepods_levels = ramp_levels(
                        inp.homepods_volume, inp.homepods_target,
                        settings.ramp_steps, settings.tiny_delta,
                    )
                    p.homepods_ramp = len(p.homepods_levels) > 1
                if p.homepods_levels:
                    reasons.append("volume:homepods_ramp" if p.homepods_ramp else "volume:homepods_direct")
            # Denon: immer hart (kein Ramp), idempotent. Gate watt-primär (FLEET-80-
            # Analogie für die Apply-Schicht): adressierbarer Player ODER physisch
            # aktiv (`denon_power_on`, an den watt-Master gebunden). Der denonavr-
            # Player meldet im Betrieb oft stale "off" (assumed-state, vgl. FLEET-83)
            # — dann ist das Ist-Volume nicht lesbar (`denon_volume` None), also nur
            # auf echte Ziel-Änderung schreiben (Anker `applied_denon`), sonst würde
            # jeder Watt-Report denselben Pegel neu setzen (Volume-OSD-Flackern).
            #
            # benni_media#16 — den Denon NIE auf 0 setzen (mirror alte Logik
            # `media_orchestrator_volumes_v4`: `dn_target > 0`). Ziel 0.0 heißt
            # „Denon soll still/aus sein", NICHT `volume_set(0)`: sonst schaltet
            # apply den frisch per HDMI-CEC eingeschalteten AVR (feste Einschalt-
            # lautstärke 0.3) im Übergangsfenster stumm, solange der Kontext noch
            # Musik ist (`denon_target = 0.0`), und muss ihn danach wieder
            # hochsetzen (belegt 03.08. 23:46: >45 s bei 0). Bei Ziel 0.0 den AVR in
            # Ruhe lassen — er wird nur auf ein positives Ziel korrigiert. Das
            # physische Aus läuft über ACTION_DENON_OFF / Nachlauf, nicht über
            # `volume_set(0)`.
            if inp.denon_configured and inp.denon_target is not None and inp.denon_target > 0:
                if inp.denon_state in PLAYER_ADDRESSABLE_VALUES:
                    denon = _direct(inp.denon_volume, inp.denon_target)
                    p.denon_set = denon[0] if denon else None
                elif inp.denon_power_on is True and (
                    state.applied_denon is None
                    or not _eq(inp.denon_target, state.applied_denon)
                ):
                    p.denon_set = round(_clamp(inp.denon_target, 0.0, 1.0), 3)
                if p.denon_set is not None:
                    reasons.append("volume:denon_set")
    else:
        reasons.append("volume:not_allowed")

    # Snapshot nach dem Restore wieder freigeben.
    if quiet_exit:
        new_state.pre_quiet_homepods = None
        new_state.pre_quiet_denon = None

    # Idempotenz-Anker für den watt-primären Denon-Pfad fortschreiben (s.o.).
    if p.denon_set is not None:
        new_state.applied_denon = p.denon_set

    # ----- Subwoofer (idempotent on/off) -----
    if inp.subwoofer_configured and inp.subwoofer_state in ("on", "off"):
        cur_on = inp.subwoofer_state == "on"
        if inp.subwoofer_allowed != cur_on:
            p.subwoofer_set = inp.subwoofer_allowed
            reasons.append("subwoofer:on" if inp.subwoofer_allowed else "subwoofer:off")

    if not p.execute:
        reasons.append("shadow:apply_disabled")
    p.reasons = reasons
    return p, new_state


# --------------------------------------------------------------------------- #
# Phase 3 — Denon-Nachlauf (R13/R14)
# --------------------------------------------------------------------------- #
# Timer-Intents: der Coordinator besitzt den realen asyncio-Countdown, die
# Pure-Logic entscheidet nur die Flanke (arm/cancel/pause) und führt das
# Armed-Buchwerk über die Ticks. Expiry-Aktion ist fix: Denon ausschalten.
TIMER_NONE: Final = "none"
TIMER_ARM: Final = "arm"
TIMER_CANCEL: Final = "cancel"
TIMER_PAUSE: Final = "pause"


@dataclass
class NachlaufState:
    """Armed-Buchwerk der Nachlauf-Timer zwischen Coordinator-Ticks (RAM)."""

    pc_armed: bool = False
    tv_armed: bool = False
    tv_paused: bool = False   # R14: während Sleep pausiert (nicht abgebrochen)
    # FLEET-80: Vortick-Power für KANTEN-getriggertes Armen (sonst Dauer-Loop:
    # PC/TV im Steady-State aus → arm → 90s → Denon aus → re-arm …). None = unbekannt.
    last_pc_on: Optional[bool] = None
    last_tv_on: Optional[bool] = None


@dataclass
class NachlaufPlan:
    """Flanken-Intent pro Timer. NONE = unverändert lassen."""

    pc: str = TIMER_NONE
    tv: str = TIMER_NONE
    reasons: list = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.pc != TIMER_NONE or self.tv != TIMER_NONE

    def as_dict(self) -> dict[str, Any]:
        return {"pc": self.pc, "tv": self.tv, "reasons": list(self.reasons)}


def decide_denon_nachlauf(
    inp: Inputs, state: Optional[NachlaufState] = None
) -> tuple[NachlaufPlan, NachlaufState]:
    """R13/R14: Denon-Nachlauf nach PC-/TV-Aus.

    R13 (PC): PC aus + Denon noch an → 90s-Timer. PC zurück (oder Denon schon
              aus / Daten unbekannt) → abbrechen. Expiry → Denon aus.
    R14 (TV): wie R13, aber **Sleep pausiert** den Timer (nicht abbrechen):
              während bio_sleep wird ein laufender Timer ausgesetzt und nach
              Sleep-Ende — falls TV weiter aus & Denon an — neu gestartet.

    Arm-Bedingung verlangt EXPLIZIT power_on==False & denon_power_on==True;
    None (unbekannt/ungebunden) armt nie und bricht einen laufenden Timer ab
    (kein Off-Schalten auf Basis fehlender Daten).

    FLEET-80 Cross-Source-Gate: Der Denon ist eine GETEILTE Senke (PC/TV/ATV/
    PS5/Switch). Ein Timer armt NUR, wenn KEIN anderer Konsument aktiv ist, und
    wird gecancelt, sobald einer dazukommt (z.B. PC aus → TV an → R13 cancel).
    `denon_consumer_active` None (unbekannt) zählt konservativ wie „aktiv"."""
    if state is None:
        state = NachlaufState()
    p = NachlaufPlan()
    ns = NachlaufState(
        pc_armed=state.pc_armed, tv_armed=state.tv_armed, tv_paused=state.tv_paused,
        last_pc_on=state.last_pc_on, last_tv_on=state.last_tv_on,
    )
    reasons: list[str] = []
    denon_on = inp.denon_power_on is True
    # Ein anderer Denon-Konsument hält den geteilten Denon. None (unbekannt) ⇒
    # konservativ wie „aktiv" → kein Off auf Basis fehlender Daten.
    # benni_media#14: über `denon_consumer_holds`, damit ein stale media_device-
    # Label eines nachweislich AUSGESCHALTETEN Konsumenten den Denon nicht hält.
    consumer_active = denon_consumer_holds(inp) is not False

    # ----- R13: PC-Aus (KANTEN-getriggert, FLEET-80) -----
    # Armen NUR auf der Fallflanke PC an→aus bei laufendem Denon UND ohne anderen
    # Konsumenten. Steady-State „PC aus" (Normalfall beim TV-Schauen) darf NICHT
    # (re-)armen → kein 90s-Loop.
    pc_off_edge = state.last_pc_on is True and inp.pc_power_on is False
    # Hält den laufenden Timer: PC aus, Denon an, KEIN anderer Konsument. Kommt
    # ein Konsument (z.B. TV) dazu, fällt pc_hold → der Timer wird gecancelt.
    pc_hold = inp.pc_power_on is False and denon_on and not consumer_active
    if pc_off_edge and denon_on and not consumer_active and not ns.pc_armed:
        p.pc = TIMER_ARM
        ns.pc_armed = True
        reasons.append("r13:arm_pc")
    elif ns.pc_armed and not pc_hold:
        # PC zurück, Denon aus ODER anderer Konsument aktiv → Timer abbrechen.
        p.pc = TIMER_CANCEL
        ns.pc_armed = False
        reasons.append("r13:cancel_pc")

    # ----- R14: TV-Aus (Sleep pausiert; KANTEN-getriggert) -----
    tv_off_edge = state.last_tv_on is True and inp.tv_power_on is False
    tv_hold = inp.tv_power_on is False and denon_on and not consumer_active
    if inp.bio_sleep is True:
        if ns.tv_armed and not ns.tv_paused:
            p.tv = TIMER_PAUSE
            ns.tv_paused = True
            reasons.append("r14:pause_sleep")
    else:
        if tv_off_edge and denon_on and not consumer_active and not ns.tv_armed:
            p.tv = TIMER_ARM
            ns.tv_armed = True
            ns.tv_paused = False
            reasons.append("r14:arm_tv")
        elif ns.tv_armed and ns.tv_paused and tv_hold:
            # Sleep-Ende, Bedingung hält → Timer neu starten (Resume, keine Flanke nötig).
            p.tv = TIMER_ARM
            ns.tv_paused = False
            reasons.append("r14:resume_tv")
        elif ns.tv_armed and not tv_hold:
            p.tv = TIMER_CANCEL
            ns.tv_armed = False
            ns.tv_paused = False
            reasons.append("r14:cancel_tv")

    # Vortick-Power fortschreiben (Flankenerkennung im nächsten Tick).
    ns.last_pc_on = inp.pc_power_on
    ns.last_tv_on = inp.tv_power_on
    p.reasons = reasons
    return p, ns


# --------------------------------------------------------------------------- #
# control#3 — Private-Time-Exit-Routing (Denon-Off-Delay + HomePod-Sperre)
# --------------------------------------------------------------------------- #
@dataclass
class PrivateExitState:
    """Buchwerk des Private-Exit-Denon-Off-Delays zwischen Ticks (RAM)."""

    was_private: bool = False
    armed: bool = False


@dataclass
class PrivateExitPlan:
    """Flanken-Intent für den Delay + HomePod-Sperre. Timer gehört dem Coordinator."""

    timer: str = TIMER_NONE          # arm/cancel/none für den Denon-Off-Delay
    suppress_homepods: bool = False  # HomePod-Start sperren, bis Denon aus
    reasons: list = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "timer": self.timer,
            "suppress_homepods": self.suppress_homepods,
            "reasons": list(self.reasons),
        }


def decide_private_exit(
    inp: "Inputs", state: Optional[PrivateExitState] = None
) -> tuple[PrivateExitPlan, PrivateExitState]:
    """control#3: Private-Time-Exit-Routing.

    Der Private-Time-STATE endet sofort in media_state/policy (audio_owner
    verlässt `private_stack`) — das verzögert dieser Delay NICHT. Hier hängt nur
    der kurze, ABBRECHBARE Denon-Off-Delay dran: endet Private mit noch
    laufendem Denon und ausgeschaltetem TV (kein anderer Konsument), wird der
    Denon nach dem Delay ausgeschaltet; solange der Delay läuft und der Denon
    noch an ist, wird der HomePod-Start gesperrt (kein kurzer Parallelbetrieb).
    Abbruch, wenn TV aktiv wird, Private neu beginnt oder ein anderer Konsument
    den Denon braucht. Separat vom generischen 90 s-Nachlauf (R13/R14)."""
    if state is None:
        state = PrivateExitState()
    p = PrivateExitPlan()
    ns = PrivateExitState(was_private=state.was_private, armed=state.armed)
    reasons: list[str] = []

    private = inp.private_active is True
    tv_on = _tv_is_off(inp) is False
    denon_on = inp.denon_power_on is True
    # benni_media#14: stale media_device darf die Exit-Flanke nicht verschlucken.
    consumer = denon_consumer_holds(inp) is not False  # None ⇒ konservativ „aktiv"
    exit_edge = state.was_private and not private

    if private:
        # Während Private kein Exit-Delay; ein laufender Delay (Re-Entry) → cancel.
        if ns.armed:
            p.timer = TIMER_CANCEL
            ns.armed = False
            reasons.append("cancel:private_reentry")
    elif exit_edge:
        if tv_on or consumer:
            # TV/anderer Konsument übernimmt den Denon → kein Delay, kein Off.
            if ns.armed:
                p.timer = TIMER_CANCEL
                ns.armed = False
            reasons.append("no_delay:tv_or_consumer")
        elif denon_on:
            p.timer = TIMER_ARM
            ns.armed = True
            reasons.append("arm:denon_off_delay")
        else:
            reasons.append("no_delay:denon_already_off")
    else:
        # Kein Private, keine Flanke — laufenden Delay pflegen/abbrechen.
        if ns.armed and (tv_on or consumer or not denon_on):
            p.timer = TIMER_CANCEL
            ns.armed = False
            reasons.append("cancel:condition_gone")

    # HomePod-Start sperren, solange der Delay läuft UND der Denon noch an ist.
    p.suppress_homepods = ns.armed and denon_on
    ns.was_private = private
    p.reasons = reasons
    return p, ns


# --------------------------------------------------------------------------- #
# Phase 4c — TV-WoL (R12): Bildschirm-Szenario → TV einschalten (ohne Debounce)
# --------------------------------------------------------------------------- #
@dataclass
class TvWolState:
    """Private R12 episode; a restored screen is never a new wake request."""

    fired: bool = False
    initialized: bool = False
    episode_active: bool = False
    episode_id: int = 0
    wol_available: bool = False
    pending: bool = False
    screen_intent: Optional[bool] = None
    last_tv_off: Optional[bool] = None
    last_consume_reason: Optional[str] = None
    last_rearm_reason: Optional[str] = None
    suppressed_reason: Optional[str] = None

    def consume(self, reason: str) -> None:
        """Invalidate even queued wake work before an intentional TV-off."""
        self.wol_available = False
        self.pending = False
        self.last_consume_reason = reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "fired": self.fired,
            "initialized": self.initialized,
            "episode_active": self.episode_active,
            "episode_id": self.episode_id,
            "screen_intent": self.screen_intent,
            "wol_available": self.wol_available,
            "wol_consumed": self.episode_active and not self.wol_available,
            "pending": self.pending,
            "last_consume_reason": self.last_consume_reason,
            "last_rearm_reason": self.last_rearm_reason,
            "suppressed_reason": self.suppressed_reason,
        }


@dataclass
class TvWolPlan:
    fire: bool = False
    reasons: list = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"fire": self.fire, "reasons": list(self.reasons)}


def denon_consumer_holds(inp: "Inputs") -> Optional[bool]:
    """Hält gerade ein ANDERER Konsument den geteilten Denon? (benni_media#14)

    Verschärfung des FLEET-80-Cross-Source-Gates gegen ein *stale* `media_device`.
    `media_device` ist ein BESCHREIBENDES Label aus media_state und hinkt der
    Power-Wahrheit um Millisekunden hinterher. Belegte Evidenz (Recorder,
    2026-07-31, Levtos/benni_media#14):

    - ``01:08:18.845`` ``sensor.benni_master_pc`` → ``off`` (``powered=false``,
      17 W) — der PC ist eindeutig aus.
    - ``01:08:25.097`` ``audio_owner`` verlässt ``private_stack`` → Private-Exit-
      Flanke. ``media_device`` steht hier NOCH auf ``pc``.
    - ``01:08:25.105`` ``media_device`` wechselt auf ``denon`` (8 ms zu spät).

    Das Gate las in diesem einen Tick „PC ist Denon-Konsument" und verwarf die
    Exit-Flanke (`no_delay:tv_or_consumer`). Die Flanke kommt nicht wieder → der
    Denon blieb dauerhaft an (``sensor.benni_master_denon`` blieb ``active``).

    Regel: Steht das Label auf einem Konsumenten, dessen EIGENE, unabhängige
    Power-Quelle explizit ``aus`` meldet, hält dieser Konsument den Denon NICHT.
    Damit entscheidet der fachliche Ist-Zustand des Konsumenten und nicht ein
    nachlaufendes Label. ``None``/unbekannt bleibt konservativ „aktiv" — die
    FLEET-80-Sicherheitslinie (kein Off auf Basis fehlender Daten) bleibt
    unangetastet, und Konsumenten ohne eigene Power-Quelle (appletv/ps5/switch)
    halten den Denon weiterhin.
    """
    active = inp.denon_consumer_active
    if active is not True:
        return active   # False (kein Konsument) / None (unbekannt) unverändert
    device = (inp.media_device or "").strip().lower()
    if device not in DENON_CONSUMER_POWER_CHECKED:
        return True
    if device == DEV_LABEL_PC and inp.pc_power_on is False:
        return False
    if device == DEV_LABEL_TV and _tv_is_off(inp) is True:
        return False
    return True


def _tv_is_off(inp: "Inputs") -> Optional[bool]:
    """R11: TV-Power. WebOS-State primär (off/standby = aus, sonst an); ist der
    Player ungebunden/unbekannt → Wattage-Fallback (tv_power_on). None = unbekannt."""
    st = inp.tv_player_state
    if st is not None and st not in ("unknown", "unavailable"):
        return st in PLAYER_OFF_VALUES
    if inp.tv_power_on is None:
        return None
    return not inp.tv_power_on


def _sleep_tv_is_off(inp: "Inputs") -> Optional[bool]:
    """Issue #59 TV evidence from the canonical TV Master only.

    A missing/unavailable master is unknown, never an off observation.  The raw
    WebOS player remains the actuator and WoL input but cannot certify sleep.
    """

    if inp.tv_power_on is None:
        return None
    return not inp.tv_power_on


def decide_tv_wol(
    inp: "Inputs", state: Optional[TvWolState] = None
) -> tuple[TvWolPlan, TvWolState]:
    """R12: one wake after a witnessed non-screen -> screen transition.

    TV-on satisfies (consumes) the episode, never re-arms it. Unknown intent
    cannot certify an episode end. Startup with a screen fails closed, without
    restoring permissions from storage or interpreting raw source metadata.
    """
    if state is None:
        state = TvWolState()
    p = TvWolPlan()
    ns = replace(state, suppressed_reason=None)
    # Existing Media State device enum; new/unrecognised values are not an end.
    screen = tv_screen_intent(inp)
    tv_off = _tv_is_off(inp)
    shutdown_edge = state.last_tv_off is False and tv_off is True
    ns.screen_intent = screen
    if tv_off is not None:
        ns.last_tv_off = tv_off

    if screen is False:
        ns.initialized = True
        ns.episode_active = False
        ns.wol_available = False
        ns.pending = False
        ns.fired = False
    elif screen is None:
        ns.consume("r12:screen_intent_unknown")
        ns.suppressed_reason = "r12:screen_intent_unknown"
    else:
        if not ns.episode_active:
            ns.episode_active = True
            ns.episode_id += 1
            ns.wol_available = ns.initialized
            if ns.wol_available:
                ns.last_rearm_reason = "r12:new_screen_episode"
            else:
                ns.consume("r12:startup_screen_unproven")
        ns.initialized = True
        if shutdown_edge:
            ns.consume("r12:tv_shutdown_edge")
        elif tv_off is False and (ns.wol_available or ns.pending):
            ns.consume("r12:tv_already_on")

    if screen is True and tv_off is True and ns.wol_available:
        p.fire = True
        ns.consume("r12:wake_requested")
        ns.fired = True
        ns.pending = True
        p.reasons.append("r12:tv_on")
    elif screen is True and tv_off is not False:
        ns.suppressed_reason = (
            "r12:tv_unknown" if tv_off is None
            else ns.last_consume_reason or "r12:episode_consumed"
        )
    if ns.suppressed_reason:
        p.reasons.append(ns.suppressed_reason)

    return p, ns


def tv_screen_intent(inp: "Inputs") -> Optional[bool]:
    """Consume the existing device enum; never arbitrate sources here."""
    if inp.media_device in SCREEN_DEVICES:
        return True
    if inp.media_device in ("none", "denon", "homepods", "pc", "ps5", "switch"):
        return False
    return None


# --------------------------------------------------------------------------- #
# Phase 3b — Sleep-TV-Off (R24): Sleep + TV läuft → 45 min → Warnung → TV aus
# --------------------------------------------------------------------------- #
@dataclass
class SleepTvState:
    """Restart-safe wall-clock state for timer and continuous off evidence."""

    armed: bool = False
    deadline: Optional[float] = None
    timer_source: Optional[str] = None
    off_confirmed_since: Optional[float] = None
    off_confirmed_at: Optional[float] = None
    last_tv_on: Optional[bool] = None
    last_bio_state: Optional[str] = None
    sleep_reference_start: Optional[str] = None
    off_commanded_for_deadline: Optional[float] = None
    warned_for_deadline: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "armed": self.armed,
            "deadline": self.deadline,
            "timer_source": self.timer_source,
            "off_confirmed_since": self.off_confirmed_since,
            "off_confirmed_at": self.off_confirmed_at,
            "last_tv_on": self.last_tv_on,
            "last_bio_state": self.last_bio_state,
            "sleep_reference_start": self.sleep_reference_start,
            "off_commanded_for_deadline": self.off_commanded_for_deadline,
            "warned_for_deadline": self.warned_for_deadline,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SleepTvState":
        if not isinstance(raw, dict):
            return cls()
        return cls(**{
            key: raw.get(key)
            for key in cls.__dataclass_fields__
            if key in raw
        })


@dataclass
class SleepTvPlan:
    """Flanken-Intent (ARM/CANCEL/EXTEND/NONE) — der Coordinator besitzt den Timer."""

    intent: str = TIMER_NONE
    reasons: list = field(default_factory=list)
    evidence: str = "inactive"

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "reasons": list(self.reasons),
            "evidence": self.evidence,
        }


TIMER_EXTEND: Final = "extend"


def decide_sleep_tv(
    inp: "Inputs",
    state: Optional[SleepTvState] = None,
    *,
    now: float = 0.0,
    delay_s: float = DEFAULT_SLEEP_TV_OFF_DELAY,
    confirm_s: float = DEFAULT_SLEEP_TV_OFF_CONFIRM,
) -> tuple[SleepTvPlan, SleepTvState]:
    """Issue #59 timer/evidence transition using absolute wall-clock deadlines."""
    if state is None:
        state = SleepTvState()
    p = SleepTvPlan()
    ns = SleepTvState.from_dict(state.as_dict())
    reasons: list[str] = []

    tv_off = _sleep_tv_is_off(inp)
    sleep_context = inp.bio_state in BIO_SLEEP_CONTEXT_VALUES
    entered_sleep_context = (
        ns.last_bio_state not in BIO_SLEEP_CONTEXT_VALUES and sleep_context
    )
    manual_ps_to_s = (
        ns.last_bio_state == BIO_PROVISIONAL_SLEEP_VALUE
        and inp.bio_state == BIO_SLEEP_VALUE
        and inp.sleep_source == "manual"
    )

    if not sleep_context:
        if ns.armed or ns.deadline is not None or ns.off_confirmed_since is not None:
            p.intent = TIMER_CANCEL
            reasons.append("issue59:sleep_context_ended")
        ns.armed = False
        ns.deadline = None
        ns.timer_source = None
        ns.off_confirmed_since = None
        ns.off_confirmed_at = None
        ns.sleep_reference_start = None
        ns.off_commanded_for_deadline = None
        ns.warned_for_deadline = None
    elif tv_off is False:
        tv_activated = ns.last_tv_on is False
        ns.off_confirmed_since = None
        ns.off_confirmed_at = None
        if manual_ps_to_s:
            ns.deadline = now + max(0.0, delay_s)
            ns.timer_source = "manual_sleep_reset"
            ns.off_commanded_for_deadline = None
            ns.warned_for_deadline = None
            p.intent = TIMER_ARM
            reasons.append("issue59:manual_sleep_reset_now_plus_45m")
        elif entered_sleep_context or tv_activated or ns.deadline is None:
            ns.deadline = now + max(0.0, delay_s)
            ns.timer_source = (
                "tv_activation" if tv_activated else "sleep_context_entry"
            )
            ns.off_commanded_for_deadline = None
            ns.warned_for_deadline = None
            p.intent = TIMER_ARM
            reasons.append(f"issue59:{ns.timer_source}")
        elif inp.sleep_tv_extend_pressed:
            ns.deadline += max(0.0, delay_s)
            ns.timer_source = "physical_extension"
            ns.off_commanded_for_deadline = None
            ns.warned_for_deadline = None
            p.intent = TIMER_EXTEND
            reasons.append("issue59:deadline_plus_45m")
        ns.armed = not (
            ns.deadline is not None
            and ns.deadline <= now
            and ns.off_commanded_for_deadline == ns.deadline
        )
        ns.sleep_reference_start = inp.sleep_reference_start
        p.evidence = "tv_active"
    elif tv_off is True:
        if ns.armed or ns.deadline is not None:
            p.intent = TIMER_CANCEL
            reasons.append("issue59:verified_tv_off")
        ns.armed = False
        ns.deadline = None
        ns.timer_source = None
        ns.off_commanded_for_deadline = None
        ns.warned_for_deadline = None
        if ns.last_tv_on is not False or ns.off_confirmed_since is None:
            ns.off_confirmed_since = now
            ns.off_confirmed_at = None
            reasons.append("issue59:off_confirmation_started")
        if now - ns.off_confirmed_since >= max(0.0, confirm_s):
            ns.off_confirmed_at = ns.off_confirmed_since + max(0.0, confirm_s)
            p.evidence = "off_confirmed"
        else:
            p.evidence = "confirming_off"
        ns.sleep_reference_start = inp.sleep_reference_start
    else:
        # A disconnect breaks continuous off evidence, but keeps an existing
        # absolute TV-off deadline so restoration cannot silently reset it.
        ns.off_confirmed_since = None
        ns.off_confirmed_at = None
        p.evidence = "unavailable"
        reasons.append("issue59:tv_unknown_not_off")

    if tv_off is not None:
        ns.last_tv_on = not tv_off
    ns.last_bio_state = inp.bio_state
    if not p.evidence:
        p.evidence = "inactive"
    if not sleep_context and p.evidence == "inactive":
        p.evidence = "inactive"

    if p.intent == TIMER_NONE and state.armed and not ns.armed:
        p.intent = TIMER_CANCEL

    p.reasons = reasons
    return p, ns


# --------------------------------------------------------------------------- #
# Phase R23 — Wake-Sequenz: Trigger-Flanke → HomePods 0.10 → Ramp auf Ziel
# --------------------------------------------------------------------------- #
@dataclass
class WakePlan:
    fire: bool = False
    reasons: list = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"fire": self.fire, "reasons": list(self.reasons)}


def decide_wake(inp: "Inputs") -> WakePlan:
    """R23: Eine steigende Flanke eines Wake-Triggers (Kaffeemaschine, Fenster,
    PS5-/PC-Ein, Private-Time) startet die Wake-Sequenz — der Coordinator setzt
    HomePods auf die Startlautstärke und rampt nach dem Debounce auf das
    media_policy-Ziel. Im Sleep unterdrückt (R25 dominant); `waking`/`awake`
    (= nicht sleep) sind erlaubt (KH-4). Stateless: Flankenerkennung im Coordinator."""
    p = WakePlan()
    if not inp.wake_trigger_fired:
        return p
    if media_block_reason(inp):
        p.reasons.append("r23:suppressed_away_gate")
        return p
    if presence_holds(inp):
        # unknown/degraded (Reconnect/Restart-Transient): kein Morgen-Radio-Start,
        # solange wir nicht wissen, ob zuhause. Laufende Musik bleibt trotzdem.
        p.reasons.append("r23:suppressed_presence_unknown")
        return p
    if inp.bio_sleep is True or inp.bio_state in BIO_SLEEP_CONTEXT_VALUES:
        p.reasons.append("r23:suppressed_sleep")
        return p
    # control#3: Private Time darf NIE eine HomePod-Wake-Sequenz ausloesen
    # (fehlerhafte R23-Altanforderung entfernt). Waehrend Private bleiben die
    # HomePods aus — hier hart geguardet, falls jemand Private als Wake-Trigger
    # verdrahtet.
    if inp.private_active is True:
        p.reasons.append("r23:suppressed_private")
        return p
    p.fire = True
    p.reasons.append("r23:wake")
    return p
