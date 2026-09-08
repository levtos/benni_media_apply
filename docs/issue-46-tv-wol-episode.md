# R12 screen / wake episode protection

Tracking: [Media Apply #46](https://github.com/Levtos/benni_media_apply/issues/46).
Version: `0.19.9`. Technical contract; installation and Live verification remain
Benni's gate.

Media Apply owns this private side-effect state. It consumes Media State's
existing `media_device` enum: `tv` / `appletv` is R12 screen intent;
`none`, `denon`, `homepods`, `pc`, `ps5`, `switch` ends that intent. Missing or
unrecognised values are unknown, never proof of an episode end. This does not
add a source priority, change the existing R12 screen-device set, or consume
LG source / Apple TV app metadata. R11 TV-power interpretation is unchanged.

| Observation | Episode / permission |
| --- | --- |
| Initial screen after startup/reload | Active, consumed; pre-existing intent is unproven |
| Confirmed non-screen | Inactive; no wake |
| Later positive screen | New episode, one permission available |
| TV already on | Consume permission; keep episode active |
| TV off with permission | Reserve exactly one immediate wake; consume before dispatch |
| TV on then off in same episode | No second wake |
| On-to-off coincides with new screen | Consume; shutdown is never a wake edge |
| Unknown device | Keep episode boundary, invalidate available/pending wake |
| Unknown TV | No wake; retain last known power for shutdown-edge protection |
| Sleep-TV-Off | Consume/invalidate before the TV-off service |

No cooldown or new timer defines the episode. Runtime state deliberately starts
without wake permission: no persisted permission can replay on reload. A screen
already present at setup therefore needs an observed end and a new start before
automatic wake works again. If upstream emits a valid non-screen followed by a
screen, Apply treats that as the canonical new intent; upstream source quality
remains Media State's responsibility.

The coordinator reserves the wake before creating its immediate task. The task
checks its episode token, current intent, confirmed TV-off and Apply gate before
each side effect. Claiming pending work before awaiting prevents duplicate
dispatch. A task from an ended episode cannot consume a newer episode. Existing
`media_player.turn_on` and optional direct WOL packet remain one bounded wake
attempt; the WebOS WOL automation stays the technical executor. No automatic
retry is introduced for failed wake service calls.

Shadow observes/consumes episodes but dispatches nothing. Enabling Apply during
that same episode cannot replay its wake. Volume and HomePod gates are unchanged.
R24's deadline, extension, persisted exactly-once marker, master evidence and
PS/S semantics are unchanged; it only invalidates R12 permission before Off.

`status().tv_wol` includes `episode_active`, `episode_id`, `screen_intent`
(true/false/null), `wol_available`, `wol_consumed`, `pending`,
`last_consume_reason`, `last_rearm_reason`, and `suppressed_reason`. The legacy
`fired` field records a reserved wake in this episode, not successful TV-on.
MAC is represented only by `mac_configured`; R12 logs contain an episode number
without a MAC or actuator entity. A dispatched service does not prove TV state.

Regression coverage includes the stale Apple-TV shutdown sequence, TV-only,
new episodes, missing inputs, reload, Shadow, real `_compute()` scheduling,
duplicate/stale tasks, rechecks between turn-on and direct packet, and the real
Sleep-TV-Off runner. Existing #41 and sleep tests remain part of the full suite.

## Benni's Live handover

After backup and installation by Benni:

1. End any screen intent left over from installation/reload. Start normal Apple
   TV screen use with TV off: at most one wake attempt, then TV on.
2. Switch TV off manually in that same episode: it must stay off, including
   while Media State briefly still reports `appletv`.
3. Exercise Sleep-/PS/S-TV-Off: it must stay off without an immediate R12 wake.
4. End screen intent completely; diagnose `episode_active=false`.
5. Start a new screen episode: exactly one new wake is permitted.

Also inspect startup with existing screen and the TV-only path. Record observed
behavior separately from technical test results; only Benni sets Live/Live
Verified. Rollback is Benni's installation of unchanged stable `v0.19.8` (which
contains the original wake defect), or a scoped revert PR and new patch release.
Never move/delete an existing stable tag. No HA action is part of this delivery.
