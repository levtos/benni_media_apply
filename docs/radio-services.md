# Native radio actions (#62, since 0.19.18)

The three former YAML scripts are replaced by native Home Assistant actions:

| Former script | Integration action | Contract |
|---|---|---|
| media_radio_start | benni_media_apply.start_radio | Request the selected station through the existing single-flight runner. |
| media_radio_stop | benni_media_apply.stop_radio | Cancel pending radio/recovery/ramp work, set the configured latch, stop the player. |
| media_radio_clear_stop_latch | benni_media_apply.clear_stop_latch | Clear only the configured latch; no playback command. |

The planned-start action returns after admission/scheduling. Playback Health remains
the completion signal. A concurrent owned start or already playing group causes no
extra start. A deliberate later request can retry a completed failed episode.
The normal Apply/Sleep/Stop/manual/owner gates apply; unlike the retired direct
script, start_radio respects the integration's Apply gate. No global control is
changed. Explicit cockpit playback keeps its existing manual command semantics.
Stop and latch release remain explicit actions usable in Shadow mode. Releasing
the latch may enable subsequent normal policy playback, but dispatches no play.

The services use configured bindings, fail visibly on missing controls, and resolve
the loaded coordinator at call time so reload cannot leave stale handlers. The
last-entry unload removes the services. The old radio_start_script option is no
longer offered or executed. Unknown stations fail visibly instead of delegating to
an optional YAML script. services.yaml contains action descriptions only; executable
radio behavior is Python-owned.

## Configuration companion

Remove the three script definitions and their package include together with the
integration update. Retire the old YAML manual-playback-end resume automation:
Trigger B has already been implemented in Apply. Waking/Awake commands set the Bio
state only; the known Bio edge already owns latch release and Wake start (#41).
Do not append a delayed second start. Bedtime delegates its radio stop to the native
action. No live installation/reload/restart is performed by this change.

## Explicit remaining migration scope

This retires the three scripts, not every radio YAML entity. Sender/target selectors,
the stop-latch helper and radio readiness/planned/manual/plan templates still exist
and have consumers in Apply/Policy. Their integration-owned entity/state migration
must preserve restored choices, latch state and consumer bindings. This remaining
work is not represented as completed. General system scripts and unrelated TV-WOL
configuration are outside this radio-script migration.
