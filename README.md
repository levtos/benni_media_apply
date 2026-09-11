# benni_media_apply

**Ausführungsschicht / Executor** der Benni-Media-Kette. Konsumiert
`benni_media_state` (Szenario) + `benni_media_policy` (Targets/Action/Gates)
**nur über HA-Entity-State** und führt sie an den echten Geräten aus — idempotent
(nur bei Ist≠Soll). HomePods geramped (16×1s, Tiny-Delta direkt); der Denon
kann keine Rampe — harter Set, und seit benni_media#13 am R2-Fenster vorbei.
Der Denon wird nie per `volume_set(0)` stummgeschaltet (nur bei Ziel > 0 gesetzt,
benni_media#16 — sonst würde der frisch per CEC eingeschaltete AVR im
Kontext-Übergangsfenster kurz auf 0 gezogen); physisches Aus läuft über die
Denon-Off-Aktion, nicht über Lautstärke 0.
Volume-Befehle gehen nur an eine spielende bzw. per `start_radio` gerade gestartete HomePod-Gruppe,
nie an eine pausierte/idle (benni_media#16 — `volume_set` weckt den AirPlay-Player
und ist dort unhörbar; Pause ist der Stop-Mechanismus, nicht `volume 0`).
HomePods-Volume/Ramp adressiert die **einzelnen Pods** (`homepods_pod_entities`,
konfigurierbar), nicht die Gruppe — Lastenheft „pro Gerät einzeln, kein Gruppen-
Call": ein `volume_set` auf die AirPlay-Sync-Gruppe weckt einen pausierten
Verbund, auf die Pods nicht. Pause/Resume/Radio bleiben auf der Gruppe. Der Ramp
ist bidirektional (Ramp-up bei steigendem, Fade-down bei sinkendem Ziel).

Muster: light_policy → scene_presets. Policy denkt, Apply tut.

## Apply-Gate (Shadow-safe)
`apply_enabled` (Option, default **OFF**) × `volume_apply_allowed` (pro
Entscheidung aus media_policy). Im Shadow wird der Apply-Plan berechnet und als
Status-Sensoren exponiert, aber **NICHT** ausgeführt. Erst einschalten, wenn der
Shadow stimmt.

## Phase 1 (FLEET-40)
HomePods-Action (pause/play; `start_radio` → delegiert an ein Script),
Volume mit Ramps, Subwoofer on/off. Restore (R20), Denon-Nachlauf (R13/R14),
Sleep-TV-Off (R24/R25), Radio-Katalog-Port, TV-WoL, FIFO-Queue folgen.

Radio-Autostart/-Resume startet nie Musik, solange ein **konkurrierender
Audio-Owner** das Audio besitzt (benni_media#16): `should_autostart_radio` blockt
bei aktivem TV **und** bei jedem Owner außer `homepods`/`none` (z. B.
`private_stack`, `tv_denon`). Ein wartender verzögerter Resume wird abgebrochen,
und unmittelbar vor dem echten Play-Kommando wird der stabile Kontext erneut
geprüft — robust gegen kurzes TV-Master-Flackern. Owner unbekannt/unbound blockt
nicht (non-regressiv).

## Wake-Playback-Recovery (#41)

Ein Wake-Start ist **single-flight**: `music_assistant.play_media` mit
`enqueue=replace` ist der einzige Start-Owner. Parallele Policy-Resumes und der
frühere verzögerte `media_player.media_play`-Impuls werden während der
Wake-Episode unterdrückt. Nach dem Start werden die einzelnen Pods explizit
entstummt.

Die Recovery erkennt den beobachteten AirPlay-Hänger über HA-Signale:
Gruppe/Pod nicht stabil `playing`, Pod `unavailable` oder
`is_volume_muted=true` bei positivem Ziel. Akustische Stille trotz vollständig
gesunder Player-States kann Home Assistant nicht beweisen.

- nach standardmäßig 60 s: einmal Soft-Recovery (vollständiger Stream-Replace +
  Unmute),
- frühestens nach 300 s: optional genau ein Music-Assistant-Neustart,
- danach 60 s warten, einmal Stream-Replace + Unmute,
- mindestens 30 min Neustart-Cooldown; kein periodischer Neustart-Loop.

Der harte Pfad ist standardmäßig aus und erfordert eine explizite
`music_assistant_app_id`. Alle verzögerten Schritte prüfen Stop-Latch, Sleep,
Policy-Gates, Präsenz, Audio-Owner, manuelle Wiedergabe und positives Ziel neu.
Status und Recovery-Stufe stehen als Sensoren und im WebSocket-Status bereit.

## Sleep-TV PS/S contract (#59)

`provisional_sleep` und `sleep` sperren automatische HomePod-Wake-Sequenzen,
lassen einen laufenden TV aber bis zur Apply-eigenen Deadline weiterlaufen.
Deadline, physische +45-Minuten-Verlängerung, manueller PS→S-Reset und ein
zehnminütiges kontinuierliches TV-Off-Fenster sind restart-fest. Unknown,
unavailable oder ein Netzwerkverlust zählen nie als TV-Aus. Der versionierte
`sensor.benni_media_apply_sleep_tv_evidence` stellt Core State ausschließlich
beobachtende Evidence bereit; Media Apply entscheidet keinen Bio-State.

Details: [Sleep-TV deadline and evidence contract](docs/sleep-tv-evidence-contract.md).

Siehe `FAHRPLAN.md`.

## Verifikation
Lokal kein HA → `py_compile` + pure-logic-Tests (`tests/test_logic.py`).
Apply-Verdrahtung nur live verifizierbar (Canary `einhornzentrale`, shadow-gated).
