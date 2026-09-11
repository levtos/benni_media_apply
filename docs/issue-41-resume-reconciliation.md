# Issue #41: TV-to-Music reconciliation

The 2026-09-07 consolidated contract extends the existing Wake recovery to
normal HomePod resume. The executor previously sent media_play and logged
executed at plan time; a successful dispatch never established actual playback.

Normal resume, policy radio and delayed radio resume claim the existing
single-flight task before dispatch. Wake cannot create a second owner while
that task runs. An owned pause records the current content ID, content type and
player reference. Normal resume first tries media_play only if not playing;
after the existing configured settling interval, the same health checker
examines the group and configured members. An unhealthy result permits exactly
one retry of that content: radio uses Music Assistant replace with the recorded
URI, while album and playlist queues receive another media_play. Only when no
content was observable does resume fall back once to the planned station.
Normal resume never escalates to the Wake-only opt-in app restart.

No timers are newly calibrated: settle defaults to 60 seconds, recheck to
30 seconds, and stable health uses three samples at 5-second intervals. Ramp,
floor, ducking and Wake unmute remain unchanged. A non-positive target does not
block the owned TV resume start, and ACTION_RESUME sends no volume command until
the group reports playing. All other start and delayed-repair safety gates stay
in place. The transient resume_allowed signal is not required after ownership,
preserving the v0.19.7 unmute fix.

The same completed/failed resume episode does not re-arm on idle/playing or
action flaps. During an owned resume, manual_playback may turn on without
cancelling while the recorded or continued content still plays. A changed
content ID, a new pause/stop, or the stop latch ends the episode. There is no
periodic playback recovery loop.

The action log distinguishes execution_requested, dispatched and executed.
For managed resume/start actions, executed remains false until health is
confirmed; failure/cancellation publishes its stage and safe reason.

Owned automatic starts with no valid media URI fail explicitly instead of
delegating to the legacy YAML script, which contains another delayed
media_play. Target and URI structure are validated before provider dispatch.
Diagnostics contain payload type, bounded scheme category, media type,
enqueue, source and target-bound status; provider exception messages and full
URIs are not logged. The exact historic provider rejection is not reconstructed
from its generic error message and is not claimed fixed independently.

Verification includes the real coordinator runner with fake HA services,
player states and a deterministic clock, plus the full existing suite.
Benni owns installation, restart and acoustic/behavioral Live verification.

## Mute capability tolerance

Players that do not advertise `VOLUME_MUTE`, or do not expose
`is_volume_muted`, are treated as not muted while their player state remains
available. They are excluded from explicit unmute calls. An unavailable or
unknown player remains unhealthy; this does not relax the playback-state gate.
