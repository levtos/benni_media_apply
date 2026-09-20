# R12 shutdown-residual protection

Tracking: [Media Apply #57](https://github.com/Levtos/benni_media_apply/issues/57).
Version: `0.19.16`.

R12 still consumes Media State's existing `media_device` enum and does not
rebuild source priority.  One existing evidence combination is now explicitly
fail-closed: raw WebOS is `off`/`standby` while the canonical TV Master still
reports `tv_power_on=true`.  This contradiction is the known shutdown residual,
not proof of a new positive screen-wake intent.

If `media_device` changes from `ps5` to `tv` in that residual state, the new
screen episode is consumed with `r12:shutdown_master_residual`; neither
`media_player.turn_on` nor a direct WOL packet may be dispatched.  Later Master
convergence to `off` cannot replay the consumed episode.

A legitimate screen start from canonical TV-off remains unchanged: the observed
non-screen to `tv`/`appletv` transition receives one wake reservation and can
dispatch at most one wake.  No timer, persisted permission, new raw binding, or
cross-repository `screen_wake_intent` contract is introduced.

Regression coverage includes the exact live `TV on + ps5 -> WebOS off -> stale
Master -> media_device tv` sequence, canonical convergence, a legitimate new
screen start, and the real coordinator scheduling path.
