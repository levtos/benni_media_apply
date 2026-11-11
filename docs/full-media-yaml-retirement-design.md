# Full Media YAML retirement — design for #64

Status: proposed; implementation is not started. The complete product assignment
is authorized by Benni. The Large-assignment design review and Go are still open.
This document is not evidence of an implemented or live migration.

## Goal and scope

Retire every remaining Media-specific YAML control, template, script and business
automation, including dependencies outside the Media package. Preserve selected
content, manual intent, state restoration, Private Time, Sleep, Wake, audio-owner,
volume, queue-position and bounded recovery contracts. Reuse current native paths;
do not introduce a second start owner, recovery runner or global priority system.

Every discovered source must have an explicit disposition: migrate, retire after
consumer proof, already native, or approved infrastructure. No undisclosed remainder.
The exact private entity/file inventory is in the configuration companion under
`docs/media-yaml-retirement-inventory.csv` and its accompanying Markdown document.

**Benni's explicit clarification:** keep the WebOS TV Wake-on-LAN automation. It is
a necessary platform integration adapter, not Media business logic to move into
Apply. Keep its configuration and existing R12 caller relationship unchanged.

HomeKit bridge exposure is separately inventoried. Proposed treatment is to keep
native HomeKit bridge configuration, with no Media decision logic in it. This is a
proposal for review, not another already authorized exception. HA documents that
some entity exposure overrides require YAML. Removing them or replacing a bridge
must not silently change accessory types, identity, pairing or operator controls:
[HomeKit Bridge documentation](https://www.home-assistant.io/integrations/homekit/).

## Evidence baseline

The audited sources are verified GitHub default-branch snapshots, not the older
local workspace branches or the September audit's obsolete snapshots:

| Repository | Source SHA |
|---|---|
| benni_media_apply | 4cca48488c3ca2c2e5fce552317b35f616bf2ff8 |
| private configuration | 3fdf4b61d363b7c7ddfb67add834b724c3a0a5d3 |
| benni_media_state | 46b0156a847dcadc6f727433672fb0478e6eade2 |
| benni_media_policy | a5f3683afef0a3365e84a42455e87ddf103f77e6 |
| benni_media | dd093f12f617add3f6160d1306fc7575864145a9 |
| plug_policy_engine | 860935b27eaed61c72e11e53638bca40ca12a47c |
| benni_light_policy | 65046808e71fedcac1eceecf457355c79c4fd82f |
| control | 848fbf5f37e0b92c8211a4cbe53188fc5cae5889 |

In the configuration snapshot there are 23 versioned YAML files. The remaining
Media package contains five helpers (two selectors, three booleans), three radio
binary sensors, one radio-plan sensor and the retained WebOS adapter. Outside it,
Media readiness/projections, system callers, legacy import definitions and HomeKit
exposure need disposition. Previously retired scripts and resume automation are
included as verification rows; they are not counted as newly implemented work.

Read-only HA inspection on 2026-11-11 still found the three legacy scripts and the
legacy resume automation loaded. A merged configuration commit is not an activated
runtime. The extra media-named sensor is provided by Spook, not by a hidden YAML
helper; it is already native. Entity-name matching alone cannot establish ownership.
API configuration searches have partial coverage for YAML-defined objects. They
must be combined with the repository include graph and a final runtime registry
check; the current evidence is not an exhaustive live-filesystem audit.

Core Devices' import file is an explicit import artifact: `services._import_file`
reads it on the import action; setup obtains definitions from ConfigEntry options.
Its presence does not prove a YAML startup dependency or that its contents equal
currently active definitions. The legacy import has three Media Combined formulas,
one Media Context master and six physical device-master definitions. Export and
compare active definitions before removing authoring rows or rebinding consumers.

## Proposed ownership and output contract

| Responsibility | Canonical owner | Migration consequence |
|---|---|---|
| Station catalogue, provider IDs and confirmed aliases | Media Apply | One catalogue, published as versioned configuration metadata; no copied URI tables in State/Policy/YAML. |
| Selected station, target and explicit persistent Stop intent | Media Apply | Integration-owned select/select/switch; preserve #41's latch/reset owner. |
| Dispatch payload readiness | Media Apply | Valid configured station/target/player only; not permission or proof of sound. |
| Playing planned/unplanned content observation | Media State | Compare player content with the catalogue/configuration metadata; do not infer a human command solely from an ID difference. |
| Start permission, selected-station plan/recommendation | Media Policy | Existing decision gates and actions; distinguish payload readiness, permission and confirmed execution. |
| Wake/Resume/Stop/Recovery side effects | Media Apply | Keep #41/#60/#62 runner and native actions; no extra YAML event path. |
| Cockpit controls and presentation | Umbrella | Use native control/actions through gateway; no direct legacy helper toggle. |
| Bias-light relay policy/protection | Plug Policy | Existing bias-light kind already follows authoritative TV/sleep; do not add another Light/Apply writer. |
| Subwoofer allowance / relay execution | Media Policy / Apply | Reuse existing allowance and execution; retire legacy projection only after consumer proof. |
| Plug-cut safety | Plug Policy | Compare legacy protection and current device hints; no raw-watt policy recalculation in Apply. |
| Device truth and physical bindings | Existing device/contract owner | Preserve Core Devices/Core Contracts boundary; no migration of device truth into Media. |
| Context and decision readiness | State / Policy | Native independent diagnostics, then aggregate in Umbrella/system consumer. |
| WebOS WoL platform adapter | HA WebOS adapter | Keep, explicitly authorized by Benni. |

Proposed native controls have stable integration unique IDs, with entity IDs resolved
through HA's registry rather than synthesized from a device display name:

- Apply station select and target select, plus an explicit radio-stop switch.
- Apply radio-catalog sensor: `schema_version`, station keys/labels, preferred and
  fallback sources, opaque confirmed aliases, metadata readiness.
- Apply radio-payload-ready binary sensor: validity and missing bindings only.
- State planned-radio-playing and unplanned-playback-active binary sensors:
  observation, content identity and source-quality attributes.
- Policy radio-plan sensor: selected key, recommendation, requested source,
  permission/block reason; never reports `executed` from a successful dispatch.
- Each module publishes its own required-source readiness; Umbrella projects these
  without rebuilding the domain calculations or publishing a second policy.

The old daily-autostart flag and retired radio-apply flag have no discovered
Media-module readers. Verify live/custom consumers, then retire them rather than
reintroducing a second daily latch or changing the integration's Apply setting.

## One-way computation and bootstrap

Apply's catalogue and user-control metadata form a configuration plane. They must
be available independently of Policy permission, Playback Health, State observation
or successful playback. State reads that metadata and raw/current player facts;
Policy reads State and user intent; Apply executes Policy. State must not use
Apply Playback Health as its identity source, as the current YAML alias mechanism
does. No Policy-to-Master calculation or readiness self-gate is introduced.

Until catalogue/control restore and required observations are proven, automatic
starts remain blocked with an explicit pending/unknown reason. A raw `playing`
state is not a substitute for confirmed content; `unknown` is not coerced to a
healthy `False`. No restart fabricates a Bio edge. Starting while the effective
volume/owner is transient before first playback retains #41's decided tolerance;
after playback the permanent safety gates remain authoritative.

## Persistent state and once-only migration

1. Add a versioned HA Store owned by Apply for selected station, target, Stop intent
   and migration phase. Reuse the existing HA storage mechanisms; do not edit
   `.storage` files, invent an external database, or keep a second writable truth.
2. On the producer-first installation, read and validate legacy runtime controls
   while they still exist. Persist the validated snapshot before marking it captured.
   Invalid/unavailable source values leave migration pending and starts blocked;
   they do not become Jack/default/off. New-install defaults are allowed only via
   an explicit fresh-install setup path, not inferred from missing legacy entities.
3. A restore reload/restart uses the native Store after capture. Repeated imports
   must not overwrite a later native selection or clear a native user Stop.
   Before authority cutover, legacy controls remain authoritative and any exposed
   native view is read-only; do not create two writable controls or dual-write loops.
4. Before committing native authority, reread the legacy controls and ensure the
   snapshot reflects edits made since initial capture. Commit the validated final
   snapshot and authority phase atomically. A crash before the commit keeps legacy
   authority; a crash after it restores native authority without another import.
5. Migrate existing options/data bindings in Apply and Policy with a versioned exact
   mapping of retired defaults. Preserve unrelated/custom bindings, report conflicts
   and missing producers. Update watches and listeners; startup order must converge
   without polling storms or a permanently blocked cyclic dependency.
6. Use different integration-owned entity/unique IDs; do not try to seize the same
   template/boolean entity IDs while old YAML is loaded. A short, explicit binding
   compatibility map is sufficient; no permanent parallel recovery/automation layer.
7. Native Stop/clear/select operations update the one Store and notify consumers.
   Keep the exact Wake reset semantics and native service names from #62. Manual
   playback and saved radio/playlist/album resume remain protected.

## Completion packages within the same assignment

These are sequential packages, not separate reduced scopes or independent owners.

| Package | Deliverable | Completion evidence |
|---|---|---|
| A | Reviewed inventory/design, exact retained adapter and infrastructure decisions | All rows dispositioned; Claude review and Benni design Go. |
| B | Apply-native controls, catalogue metadata, persistence and import preview/commit | Restore/crash/reload tests and no action on unproven state. |
| C | State observations/readiness, Policy plan and binding migration, Umbrella controls | Cross-module tests, no copied catalogue and no feedback self-gate. |
| D | System readiness consumers, import-projection/Plug/Light boundary resolution | Active definition/binding comparison; regression tests for the currently authoritative behavior. |
| E | Configuration deletion and compatibility cleanup | Remove Media helpers/templates/includes and obsolete authoring rows only after producer/consumer readiness. Keep WebOS adapter. |
| F | Implementation review, release Go and stable delivery | Green required checks, reviewed PRs, verified manifests/tags/actions/HACS, configuration companion. |
| G | Benni activation and audible/live acceptance | Runtime old-entity/reference check and real Wake/Resume/Stop verification; Live is separate. |

Source paths expected to change: Apply setup/platforms/const/config flow/coordinator/
services/storage; State coordinator/logic/entities and bindings; Policy coordinator/
logic/entities/config-entry migration; Umbrella gateway/types/control presentation;
private configuration Media package, system readiness and documentation/import
rows. Plug/Light changes are conditional on current consumer evidence, not an
excuse to silently change their existing native behavior. This design authorizes
neither a general Core Contracts cutover nor unrelated state-architecture decisions.

Existing native Private Time, volume matrices, Nudge/Boost, ramps, Denon timers,
Sleep-TV deadlines and the three radio services are coverage items, not instructions
to recreate old Lastenheft YAML or add missing new product features. Verify their
current native ownership and keep their behavior. No six-hour stream restart,
playlist feature or new station matrix is introduced by YAML retirement.

## Retiring legacy import semantics

The imported Media Context master and Media State represent different historical
contracts. Likewise the legacy bias-light formula differs from the native Plug
bias-light policy. Names alone do not prove equivalence. Inspect actual published
outputs, configured consumers and current versions. If a legacy formula is unused,
retire its authoring/config consumer links with evidence. If it is active, preserve
its observed contract in its owning module and obtain review for any semantic
conflict; do not select a new desired behavior by implementation accident.

Physical device masters already belong to a native integration. Keep their runtime
ownership and custom raw bindings. Removing a Media bootstrap section must not
invoke the file's remove/replace actions on the live instance. Do not delete native
masters before their approved published-contract successor is live verified.
The private inventory records this boundary instead of excluding those files.

## Validation plan

- Restore matrix: chosen station differs from Jack, Stop on/off, target choice,
  unavailable/malformed legacy sources, no legacy source on an explicit new install,
  reload, crash before/after snapshot commit, repeat migration, post-import edits.
- Binding/registry matrix: old defaults in options and data, custom bindings,
  renamed/device-prefixed entities, both installation profiles, missing producer,
  legacy/native co-existence, different module load orders, unload/reload listeners.
- Radio: all six source pairs, confirmed/ambiguous library aliases, foreign content,
  manual chosen station, stream failure with stale playing, initial/soft/hard path,
  Stop/Sleep/Private/Away cancellation, zero/unknown inputs and no duplicate start.
- State/Policy: observed planned/unplanned versus true manual intent, policy
  permission versus readiness, recommendation versus selection, no self-latch or
  new screen/quiet/restore behavior.
- Native functional regression: existing Apply `test_resume_reconciliation`,
  `test_radio_provider_fallback`, `test_radio_services`, `test_stuck_mute_recovery`,
  `test_tv_wol_episode`, ramp and Sleep-TV tests; State `test_rebind`,
  `test_source_coordinator`, `test_source_arbitration`, presence/logic tests; Policy
  logic and subwoofer tests; Plug engine/device preset tests when affected; Light
  migration/policy tests if consumers change. Add meaningful cross-module contract
  tests for the new configuration plane and staged migration.
- Repository YAML scan and private inventory coverage must find no unclassified
  Media business/control definition. Runtime registry/reference searches and
  non-versioned live files remain a final Benni activation gate, not an offline claim.
- This design-only PR needs document/CSV/source-locator/link validation; no runtime
  test suite or HACS release is claimed for a documentation change.

## Activation, rollback and open review gates

Install producers while legacy YAML remains active, verify captured state and native
metadata, then compatible consumers. Commit native authority only after the consumer
migration report is ready; remove YAML in the companion and activate/restart only
through Benni. A combined code update plus immediate deletion/restart without a
captured snapshot is not an acceptable migration procedure.

Rollback uses normal revert PRs/releases and the preserved migration snapshot. If
native controls changed after cutover, exporting their latest values before restoring
old helpers is mandatory; an old legacy snapshot must not overwrite those changes.
No force-push, old tag replacement, direct storage edit or automatic live rollback.

Open gates: actual active legacy import/custom consumers; exact native contract
review and bootstrap ordering; HomeKit exposure disposition; Claude design review
and Benni Go. Claude CLI is not available in this workspace, so no Claude review
has been fabricated or substituted with an unlabelled self-review. After design Go,
implementation remains the same complete #64 assignment and must reach its own
review/release gates. Tests Pass, a merged design, or published packages are not Live.

References: [#64](https://github.com/Levtos/benni_media_apply/issues/64),
[#41](https://github.com/Levtos/benni_media_apply/issues/41),
[#60](https://github.com/Levtos/benni_media_apply/issues/60),
[#62](https://github.com/Levtos/benni_media_apply/issues/62),
[consumer-cutover context](https://github.com/Levtos/control/issues/7),
[Large assignment gates](https://github.com/Levtos/control/blob/main/docs/workflow/working-rules.md#size-of-an-assignment).
