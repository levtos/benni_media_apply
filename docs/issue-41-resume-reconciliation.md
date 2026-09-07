# Issue #41: TV-to-Music reconciliation

The 2026-09-07 consolidated contract extends the existing Wake recovery to
normal HomePod resume. The executor previously sent media_play and logged
executed at plan time; a successful dispatch never established actual playback.

Normal resume, policy radio and delayed radio resume claim the existing
single-flight task before dispatch. Wake cannot create a second owner while
that task runs. Normal resume first tries media_play only if not playing;
after the existing configured settling interval, the same health checker
examines the group and configured members. An unhealthy result permits one
controlled Music Assistant replace, followed by the existing recheck. Normal
resume never escalates to the Wake-only opt-in app restart.

No timers are newly calibrated: settle defaults to 60 seconds, recheck to
30 seconds, and stable health uses three samples at 5-second intervals. Ramp,
floor, ducking and Wake unmute remain unchanged. Start admission and every
delayed repair retain the existing safety gates. The transient resume_allowed
signal is not required after ownership, preserving the v0.19.7 unmute fix.

The same completed/failed resume episode does not re-arm on idle/playing or
action flaps. A real interruption or changed planned station permits a new
episode. There is no periodic playback recovery loop.

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
