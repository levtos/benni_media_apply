"""benni_media#16 — HomePods-Pause vs. Volume-Ramp auf dem Gruppen-Player.

Bewiesener Live-Befund (Recorder + System-Log, 2026-08-01, Musik→TV, stabiler
TV-Kontext ab 15:28:14):

    15:28:14  action pause_homepods, HP-Target 0.0   (Pause geplant)
    15:28:22  Gruppe `idle`  (MA hat pausiert)
    ...       Volume-Rampe lief WEITER gegen 0.0 auf der pausierten Gruppe:
              0.42 → 0.39 → … → 0.01 im 1-s-Takt
    15:28:39  Gruppe → `playing` (bei vol 0.12)  ← volume_set weckte den Player
    15:28:45  Gruppe → `playing` (bei vol 0.07)  ← erneut
    → pause_homepods feuerte 3×, Gruppe flappte trotz stabilem TV-Kontext.
    System-Log: `media_player.volume_set volume=0.0` auf die Gruppe scheiterte.

Kern: Ein AirPlay-/MA-Gruppen-Player nimmt `volume_set` nicht neutral entgegen —
auf einem pausierten/idle Player weckt es die Wiedergabe. Ein Volume-Befehl ist
dort ohnehin unhörbar. Fix: Volume nur auf eine spielende bzw. gerade gestartete
Gruppe; beim Pause nie (Pause ist der Stop-Mechanismus, nicht volume 0).

Die absichtliche 16×1-s-Wake/Resume-Rampe bleibt vollständig erhalten (sie läuft
über resume/start_radio).
"""
from __future__ import annotations

import bma_const as C
import bma_logic as L


def _inp(**kw):
    base = dict(
        apply_enabled=True,
        volume_apply_allowed=True,
        action=C.ACTION_NONE,
        homepods_configured=True,
        homepods_state="playing",
        homepods_volume=0.45,
        homepods_target=None,
        denon_configured=True,
        denon_state="on",
        denon_volume=0.3,
        denon_target=None,
        subwoofer_configured=True,
        subwoofer_state="off",
        subwoofer_allowed=False,
    )
    base.update(kw)
    return L.Inputs(**base)


def _plan(inp, state=None, settings=None):
    return L.decide_apply(inp, state, settings)[0]


# --------------------------------------------------------------------------- #
# 1. Der Helper (reine Regel)
# --------------------------------------------------------------------------- #
def test_helper_blocks_volume_on_pause():
    assert L.homepods_volume_addressable(C.ACTION_PAUSE, "playing") is False


def test_helper_blocks_volume_on_idle_without_start():
    assert L.homepods_volume_addressable(C.ACTION_NONE, "idle") is False
    assert L.homepods_volume_addressable(C.ACTION_NONE, "paused") is False


def test_helper_allows_volume_while_playing():
    assert L.homepods_volume_addressable(C.ACTION_NONE, "playing") is True


def test_helper_allows_wake_start_but_blocks_paused_resume():
    assert L.homepods_volume_addressable(C.ACTION_START_RADIO, "idle") is True
    assert L.homepods_volume_addressable(C.ACTION_RESUME, "paused") is False


# --------------------------------------------------------------------------- #
# 2. Musik → TV: genau EINE Pause, KEIN Volume-Ramp auf die Gruppe
# --------------------------------------------------------------------------- #
def test_pause_carries_no_volume_ramp():
    """Der belegte TV-Start: pause + HP-Target 0.0. Die Pause bleibt, aber die
    Abwärts-Rampe gegen 0 (die den Player weckte) entfällt komplett."""
    p = _plan(_inp(
        action=C.ACTION_PAUSE, homepods_should_pause=True,
        homepods_state="playing", homepods_volume=0.45, homepods_target=0.0,
    ))
    assert p.homepods_action == C.ACTION_PAUSE      # expliziter media_pause-Pfad
    assert p.homepods_levels == []                  # KEIN volume_set(0) als Ersatz
    assert p.homepods_ramp is False


def test_pause_is_stop_not_volume_zero():
    """#16-Kernsemantik: Pause ≠ volume 0. Auch bei Target 0.0 kein Volume-Leg."""
    p = _plan(_inp(action=C.ACTION_PAUSE, homepods_should_pause=True,
                   homepods_state="playing", homepods_target=0.0))
    assert "volume:homepods_ramp" not in p.reasons
    assert "volume:homepods_direct" not in p.reasons


def test_idle_group_under_tv_gets_no_volume():
    """Fenster 15:28:22→15:28:39: Gruppe idle, action none, Target hält 0.0 —
    die Abwärts-Rampe auf den idle-Player (die ihn weckte) entfällt."""
    p = _plan(_inp(action=C.ACTION_NONE, homepods_state="idle",
                   homepods_volume=0.30, homepods_target=0.0))
    assert p.homepods_levels == []


def test_stable_tv_context_idle_group_is_a_noop():
    """Stabiler TV-Kontext, Gruppe bereits idle: nichts zu tun (Idempotenz)."""
    p = _plan(_inp(action=C.ACTION_NONE, homepods_state="idle",
                   homepods_target=0.0))
    assert p.homepods_action == C.ACTION_NONE
    assert p.homepods_levels == []
    assert p.has_work is False


def test_repeated_pause_ticks_never_emit_volume():
    """Wiederholte/verspätete State-Events unter TV dürfen keinen Volume-Strom
    erzeugen — egal ob die Gruppe gerade playing oder idle gemeldet wird."""
    for state in ("playing", "idle", "paused", "playing", "idle"):
        should_pause = state == "playing"
        action = C.ACTION_PAUSE if should_pause else C.ACTION_NONE
        p = _plan(_inp(action=action, homepods_should_pause=should_pause,
                       homepods_state=state, homepods_target=0.0))
        assert p.homepods_levels == [], state


# --------------------------------------------------------------------------- #
# 3. Die gewollte Wake/Resume-Rampe bleibt vollständig erhalten
# --------------------------------------------------------------------------- #
def test_start_radio_keeps_the_full_wake_ramp():
    """TV→Musik: start_radio auf die idle Gruppe rampt weiter hoch (16 Schritte)."""
    p = _plan(_inp(action=C.ACTION_START_RADIO, homepods_state="idle",
                   homepods_volume=0.05, homepods_target=0.45))
    assert p.homepods_action == C.ACTION_START_RADIO
    assert p.homepods_ramp is True
    assert len(p.homepods_levels) == 16
    assert p.homepods_levels[-1] == 0.45


def test_resume_from_paused_waits_for_playing_before_ramp():
    p = _plan(_inp(action=C.ACTION_RESUME, homepods_resume_allowed=True,
                   homepods_state="paused", homepods_volume=0.05,
                   homepods_target=0.45))
    assert p.homepods_action == C.ACTION_RESUME
    assert p.homepods_ramp is False
    assert p.homepods_levels == []


def test_playing_group_still_ramps_normally():
    """Kein Regress: eine spielende Gruppe rampt Volume wie bisher."""
    p = _plan(_inp(action=C.ACTION_NONE, homepods_state="playing",
                   homepods_volume=0.2, homepods_target=0.5))
    assert p.homepods_ramp is True
    assert p.homepods_levels[-1] == 0.5


# --------------------------------------------------------------------------- #
# 4. Quiet-Ducking: nur auf spielender Gruppe
# --------------------------------------------------------------------------- #
def test_quiet_duck_on_playing_group_unchanged():
    p = _plan(_inp(quiet_mode=True, homepods_state="playing",
                   homepods_volume=0.5, homepods_target=0.10))
    assert p.homepods_ramp is False
    assert p.homepods_levels == [0.1]


def test_quiet_duck_suppressed_on_idle_group():
    """Ducking auf einer idle Gruppe würde sie nur wecken — unterbleibt."""
    p = _plan(_inp(quiet_mode=True, homepods_state="idle",
                   homepods_volume=0.5, homepods_target=0.10))
    assert p.homepods_levels == []


# --------------------------------------------------------------------------- #
# 5. Schnelle Wechsel TV → Musik → TV bleiben deterministisch
# --------------------------------------------------------------------------- #
def test_fast_tv_music_tv_sequence():
    # TV übernimmt (Gruppe spielt) → genau Pause, kein Volume
    tv1 = _plan(_inp(action=C.ACTION_PAUSE, homepods_should_pause=True,
                     homepods_state="playing", homepods_target=0.0))
    assert tv1.homepods_action == C.ACTION_PAUSE and tv1.homepods_levels == []
    # Musik zurück (Gruppe idle) → start_radio mit Wake-Ramp
    music = _plan(_inp(action=C.ACTION_START_RADIO, homepods_state="idle",
                       homepods_volume=0.05, homepods_target=0.45))
    assert music.homepods_action == C.ACTION_START_RADIO
    assert len(music.homepods_levels) == 16
    # TV erneut (Gruppe spielt wieder) → wieder nur Pause
    tv2 = _plan(_inp(action=C.ACTION_PAUSE, homepods_should_pause=True,
                     homepods_state="playing", homepods_target=0.0))
    assert tv2.homepods_action == C.ACTION_PAUSE and tv2.homepods_levels == []


# --------------------------------------------------------------------------- #
# 6. Explizites HomePod-Ziel (kein impliziter „aktiver Player")
# --------------------------------------------------------------------------- #
def test_homepods_target_prefill_is_the_ma_group():
    """Rückweg zur Musik zielt explizit auf die HomePod-MA-Gruppe."""
    assert (
        C.PROFILE_PREFILL[C.DEFAULT_PROFILE][C.CONF_HOMEPODS_PLAYER]
        == "media_player.living_homepods_ma_group"
    )
