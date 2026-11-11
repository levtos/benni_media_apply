# Radio starts and provider fallback (#60)

Jack FM Deutschland is the default/recommended station. The existing selector key
`jack_fm_berlin` stays compatible with restored choices. A selected station always
wins; changing the default does not overwrite an existing selector value.

Apply owns the radio catalogue and all automatic start/recovery side effects.
Radio Browser is the preferred source. Each of the six catalogue stations has a
TuneIn alternative; domain URIs let MA select its loaded provider instance without
publishing an installation-specific ID. The existing cockpit shortcut API returns
both source buttons per station, with Jack first. Manual play cancels pending
recovery before dispatch and remains independent of the automatic shadow gate.

Before an automatic start, only the selected station is refreshed through MA
search (five seconds total budget, five minute cache). The same resolver supplies
the cockpit. A unique exact-name RB result can replace an old primary URI; ambiguous
or missing results retain the verified baseline. Exact-name library results are
opaque same-station aliases, never converted into Radio Browser IDs. Duplicate
library names are not accepted as identity evidence. TuneIn alternatives match the
known provider station ID, regardless of the configured instance prefix.

A provider dispatch failure remains unhealthy even if stale players report
`playing`. After a successful call, the group's content ID must match the requested
station (or an observed alias), in addition to the existing stable group/member,
mute and volume checks. `radio_start_error` and sanitized `radio_provider_errors`
keep failures diagnosable without copying provider exception payloads. Acoustic
silence with otherwise healthy signals remains outside this proof.

The existing single-flight runner performs provider fallback in its one soft
replacement slot. Explicit dispatch failure skips the long settling wait. A
successful dispatch without confirmed playback can use the same fallback after
settling. Mute-only failures keep the source. Normal resume ends after this bounded
attempt; the existing opt-in hard recovery remains Wake-only. There is no new
recovery task, periodic restart or retry loop. Safety is checked again before each
side effect, including after search and settling. The #41 tolerance for transient
zero volume/owner `none` before the first playback remains intact.

Screen/Away resume first continues the saved content with `media_play`. For a known
RB radio, its soft retry may use TuneIn for that same station; it never selects the
current plan instead of the saved station. Unknown radio URIs retain the existing
same-URI retry. Albums/playlists retain queue-position resume semantics.

## Configuration companion and Live gate

The private HA configuration must use the corrected primary IDs and classify both
providers as the same planned station. Its planned/manual templates consume the
additive `radio_content_ids` attribute on Playback Health for runtime search aliases.
Deploy the configuration companion together with this integration; old exact-RB-only
templates would misclassify TuneIn/library playback as manual. The manual YAML
start script must not add another delayed `media_play` after `play_media(replace)`.
Since 0.19.18 that script is retired entirely; see [Native radio actions](radio-services.md).

On an installation restoring GAYFM, Benni selects Jack once as part of activation.
Do not add an `initial` value that would reset an intentionally chosen station on
every HA restart. Installation/reload/restart and audible Wake/Resume checks remain
Benni's Live/Live Verified gate. Tests, merge, stable release and catalogue metadata
are technical evidence only.

## Metadata evidence, 2026-11-11

All six configured RB IDs were present in the public Radio Browser API and had
`lastcheckok=1`; all six TuneIn station IDs resolved to the intended names. Jack's
RB entry is German and links to jackfm.de; TuneIn s84493 is Jack FM Deutschland.
WDR2's existing direct RB UUID was verified directly; library radio 3 was not used
as a substitute provider UUID. Four primary IDs differ from the old catalogue;
this does not prove every former ID is permanently invalid (the old NDR ID remains
listed). MA search may wrap stations in library URIs and return multiple RB entries.
Provider metadata and mocked service tests do not prove real audio delivery.
