"""Config- und Options-Flow für benni_media_apply.

Profil-Mechanik 1:1 aus benni_core_state/-policy (gelockte Blaupause):
- `user`: Profil-SelectSelector (benni/eltern).
- `entities`: Quell-/Target-Slots (media_policy/state-Eingänge + eigene Geräte),
  vorbefüllt mit der Profil-Map; gespeichert werden nur Abweichungen.
- `options`: Apply-Gate (Shadow) + Ramp-Settings + Radio-Start-Script.
- Single-Instance; Auto-Bind lebt im Coordinator.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_APPLY_ENABLED,
    CONF_DEBOUNCE_SECONDS,
    CONF_DENON_NACHLAUF_PC,
    CONF_DENON_NACHLAUF_TV,
    CONF_PRIVATE_EXIT_DELAY,
    CONF_DENON_PLAYER,
    CONF_DUCKED_LEVEL,
    CONF_HOMEPODS_PLAYER,
    CONF_HOMEPODS_PODS,
    CONF_MUSIC_ASSISTANT_APP_ID,
    CONF_MUSIC_ASSISTANT_RESTART_COOLDOWN,
    CONF_MUSIC_ASSISTANT_RESTART_WAIT,
    CONF_PLAYBACK_HARD_RECOVERY,
    CONF_PLAYBACK_RECOVERY,
    CONF_PLAYBACK_RECOVERY_HARD_AFTER,
    CONF_PLAYBACK_RECOVERY_SETTLE,
    CONF_PROFILE,
    CONF_RADIO_AUTOSTART,
    CONF_RADIO_RESUME_DELAY,
    CONF_RAMP_STEP_DELAY,
    CONF_RAMP_STEPS,
    CONF_SLEEP_TV_NOTIFY,
    CONF_SLEEP_TV_OFF_DELAY,
    CONF_SLEEP_TV_WARN_LEAD,
    CONF_SLEEP_TV_WARN_MESSAGE,
    CONF_SUBWOOFER_SWITCH,
    CONF_TINY_DELTA,
    CONF_TV_PLAYER,
    CONF_TV_WOL_MAC,
    CONF_WAKE_DEBOUNCE,
    CONF_WAKE_START_VOLUME,
    CONF_WAKE_TRIGGERS,
    DEFAULT_APPLY_ENABLED,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_DENON_NACHLAUF_PC,
    DEFAULT_DENON_NACHLAUF_TV,
    DEFAULT_PRIVATE_EXIT_DELAY,
    DEFAULT_DUCKED_LEVEL,
    DEFAULT_MUSIC_ASSISTANT_APP_ID,
    DEFAULT_MUSIC_ASSISTANT_RESTART_COOLDOWN,
    DEFAULT_MUSIC_ASSISTANT_RESTART_WAIT,
    DEFAULT_PLAYBACK_HARD_RECOVERY,
    DEFAULT_PLAYBACK_RECOVERY,
    DEFAULT_PLAYBACK_RECOVERY_HARD_AFTER,
    DEFAULT_PLAYBACK_RECOVERY_SETTLE,
    DEFAULT_PROFILE,
    DEFAULT_RADIO_AUTOSTART,
    DEFAULT_RADIO_RESUME_DELAY,
    DEFAULT_SLEEP_TV_NOTIFY,
    DEFAULT_SLEEP_TV_OFF_DELAY,
    DEFAULT_SLEEP_TV_WARN_LEAD,
    DEFAULT_SLEEP_TV_WARN_MESSAGE,
    DEFAULT_TV_WOL_MAC,
    DEFAULT_WAKE_DEBOUNCE,
    DEFAULT_WAKE_START_VOLUME,
    DEFAULT_RAMP_STEP_DELAY,
    DEFAULT_RAMP_STEPS,
    DEFAULT_TINY_DELTA,
    DOMAIN,
    ENTITY_SLOT_KEYS,
    LEGACY_ENTITY_MAP,
    NAME,
    PROFILE_LABELS,
    PROFILE_PREFILL,
    PROFILES,
)

# --- Selektoren ---
_ENTITY = selector.EntitySelector(selector.EntitySelectorConfig())
_PLAYER = selector.EntitySelector(selector.EntitySelectorConfig(domain="media_player"))
_SWITCH = selector.EntitySelector(selector.EntitySelectorConfig(domain="switch"))
_BOOL = selector.BooleanSelector()

_PLAYER_KEYS = (CONF_HOMEPODS_PLAYER, CONF_DENON_PLAYER, CONF_TV_PLAYER)
_SWITCH_KEYS = (CONF_SUBWOOFER_SWITCH,)
_MULTI = selector.EntitySelector(selector.EntitySelectorConfig(multiple=True))
_MULTI_KEYS = (CONF_WAKE_TRIGGERS,)
# benni_media#16 — Pod-Liste: mehrere media_player (die einzelnen AirPlay-Pods).
_MULTI_PLAYER = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="media_player", multiple=True)
)
_MULTI_PLAYER_KEYS = (CONF_HOMEPODS_PODS,)


def _normalize_entity_value(value: Any) -> Any:
    if isinstance(value, str):
        return LEGACY_ENTITY_MAP.get(value, value)
    if isinstance(value, list):
        return [LEGACY_ENTITY_MAP.get(item, item) if isinstance(item, str) else item for item in value]
    return value


def _selector_for(key: str) -> Any:
    if key in _MULTI_PLAYER_KEYS:
        return _MULTI_PLAYER
    if key in _MULTI_KEYS:
        return _MULTI
    if key in _PLAYER_KEYS:
        return _PLAYER
    if key in _SWITCH_KEYS:
        return _SWITCH
    return _ENTITY


_RAMP_FIELDS: dict[str, tuple[Any, Any]] = {
    CONF_RAMP_STEPS: (DEFAULT_RAMP_STEPS, vol.All(vol.Coerce(int), vol.Range(min=1, max=64))),
    CONF_RAMP_STEP_DELAY: (DEFAULT_RAMP_STEP_DELAY, vol.All(vol.Coerce(float), vol.Range(min=0.1, max=10.0))),
    CONF_TINY_DELTA: (DEFAULT_TINY_DELTA, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=0.5))),
    CONF_DUCKED_LEVEL: (DEFAULT_DUCKED_LEVEL, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0))),
    # R2 — Debounce-Fenster (Sekunden, 0 = sofort). Quiet bricht ohnehin durch.
    CONF_DEBOUNCE_SECONDS: (DEFAULT_DEBOUNCE_SECONDS, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=60.0))),
    # Phase 3 — Denon-Nachlauf (R13/R14), Sekunden.
    CONF_DENON_NACHLAUF_PC: (DEFAULT_DENON_NACHLAUF_PC, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=600.0))),
    CONF_DENON_NACHLAUF_TV: (DEFAULT_DENON_NACHLAUF_TV, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=600.0))),
    # control#3 — Private-Exit-Denon-Off-Delay (separat vom 90 s-Nachlauf).
    CONF_PRIVATE_EXIT_DELAY: (DEFAULT_PRIVATE_EXIT_DELAY, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=120.0))),
    # FLEET-79 — Radio-Autostart: Resume-Delay nach manueller Wiedergabe.
    CONF_RADIO_RESUME_DELAY: (DEFAULT_RADIO_RESUME_DELAY, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=120.0))),
    # #41 — gestufte Wake-Playback-Recovery.
    CONF_PLAYBACK_RECOVERY_SETTLE: (
        DEFAULT_PLAYBACK_RECOVERY_SETTLE,
        vol.All(vol.Coerce(float), vol.Range(min=15.0, max=300.0)),
    ),
    CONF_PLAYBACK_RECOVERY_HARD_AFTER: (
        DEFAULT_PLAYBACK_RECOVERY_HARD_AFTER,
        vol.All(vol.Coerce(float), vol.Range(min=60.0, max=900.0)),
    ),
    CONF_MUSIC_ASSISTANT_RESTART_WAIT: (
        DEFAULT_MUSIC_ASSISTANT_RESTART_WAIT,
        vol.All(vol.Coerce(float), vol.Range(min=15.0, max=300.0)),
    ),
    CONF_MUSIC_ASSISTANT_RESTART_COOLDOWN: (
        DEFAULT_MUSIC_ASSISTANT_RESTART_COOLDOWN,
        vol.All(vol.Coerce(float), vol.Range(min=300.0, max=21600.0)),
    ),
    # Phase 3b — Sleep-TV-Off (R24), Sekunden.
    CONF_SLEEP_TV_OFF_DELAY: (DEFAULT_SLEEP_TV_OFF_DELAY, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=21600.0))),
    CONF_SLEEP_TV_WARN_LEAD: (DEFAULT_SLEEP_TV_WARN_LEAD, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=600.0))),
    # R23 — Wake-Sequenz.
    CONF_WAKE_START_VOLUME: (DEFAULT_WAKE_START_VOLUME, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0))),
    CONF_WAKE_DEBOUNCE: (DEFAULT_WAKE_DEBOUNCE, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=60.0))),
}


def _entities_schema(defaults: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {}
    for key in ENTITY_SLOT_KEYS:
        d = defaults.get(key)
        marker = vol.Optional(key, default=d) if d else vol.Optional(key)
        fields[marker] = _selector_for(key)
    return vol.Schema(fields)


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Optional(
            CONF_APPLY_ENABLED,
            default=bool(defaults.get(CONF_APPLY_ENABLED, DEFAULT_APPLY_ENABLED)),
        ): _BOOL,
        vol.Optional(
            CONF_RADIO_AUTOSTART,
            default=bool(defaults.get(CONF_RADIO_AUTOSTART, DEFAULT_RADIO_AUTOSTART)),
        ): _BOOL,
        vol.Optional(
            CONF_PLAYBACK_RECOVERY,
            default=bool(defaults.get(CONF_PLAYBACK_RECOVERY, DEFAULT_PLAYBACK_RECOVERY)),
        ): _BOOL,
        vol.Optional(
            CONF_PLAYBACK_HARD_RECOVERY,
            default=bool(
                defaults.get(CONF_PLAYBACK_HARD_RECOVERY, DEFAULT_PLAYBACK_HARD_RECOVERY)
            ),
        ): _BOOL,
    }
    for key, (default, coerce) in _RAMP_FIELDS.items():
        fields[vol.Optional(key, default=defaults.get(key, default))] = coerce
    app_id_default = defaults.get(
        CONF_MUSIC_ASSISTANT_APP_ID, DEFAULT_MUSIC_ASSISTANT_APP_ID
    )
    fields[vol.Optional(CONF_MUSIC_ASSISTANT_APP_ID, default=app_id_default)] = (
        selector.TextSelector(selector.TextSelectorConfig())
    )
    # R12 — TV-WoL: variable MAC (leer = nur turn_on / webOS-Leuchtfeuer).
    mac_default = defaults.get(CONF_TV_WOL_MAC, DEFAULT_TV_WOL_MAC)
    fields[vol.Optional(CONF_TV_WOL_MAC, default=mac_default)] = selector.TextSelector(
        selector.TextSelectorConfig()
    )
    # R24 — Sleep-TV-Off: Warn-Notify-Service + Warntext (Text, optional).
    notify_default = defaults.get(CONF_SLEEP_TV_NOTIFY, DEFAULT_SLEEP_TV_NOTIFY)
    fields[vol.Optional(CONF_SLEEP_TV_NOTIFY, default=notify_default)] = selector.TextSelector(
        selector.TextSelectorConfig()
    )
    warn_default = defaults.get(CONF_SLEEP_TV_WARN_MESSAGE, DEFAULT_SLEEP_TV_WARN_MESSAGE)
    fields[vol.Optional(CONF_SLEEP_TV_WARN_MESSAGE, default=warn_default)] = selector.TextSelector(
        selector.TextSelectorConfig()
    )
    return vol.Schema(fields)


def _entity_overrides(profile: str, user_input: dict[str, Any]) -> dict[str, Any]:
    """Nur echte Abweichungen vom Profil-Map als Override speichern."""
    code = PROFILE_PREFILL.get(profile, {})
    out: dict[str, Any] = {}
    for key in ENTITY_SLOT_KEYS:
        v = _normalize_entity_value(user_input.get(key))
        if v and v != code.get(key):
            out[key] = v
    return out


def _override_or_map(profile: str, data: dict[str, Any]) -> dict[str, Any]:
    code = PROFILE_PREFILL.get(profile, {})
    out: dict[str, Any] = {}
    for key in ENTITY_SLOT_KEYS:
        v = _normalize_entity_value(data.get(key)) or code.get(key)
        if v:
            out[key] = v
    return out


def _profile_schema(default: str) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_PROFILE, default=default): selector.SelectSelector(
            selector.SelectSelectorConfig(
                mode=selector.SelectSelectorMode.LIST,
                options=[
                    selector.SelectOptionDict(value=p, label=PROFILE_LABELS[p])
                    for p in PROFILES
                ],
            )
        )
    })


class MediaApplyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._profile: str = DEFAULT_PROFILE
        self._entities: dict[str, Any] = {}

    def _prefill_defaults(self) -> dict[str, Any]:
        prefill = PROFILE_PREFILL.get(self._profile, {})
        return {
            key: eid
            for key, eid in prefill.items()
            if isinstance(eid, str) and self.hass.states.get(eid) is not None
        }

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=_profile_schema(DEFAULT_PROFILE),
            )
        self._profile = user_input[CONF_PROFILE]
        return await self.async_step_entities()

    async def async_step_entities(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="entities", data_schema=_entities_schema(self._prefill_defaults()),
            )
        self._entities = _entity_overrides(self._profile, user_input)
        return await self.async_step_options()

    async def async_step_options(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="options", data_schema=_options_schema({}),
            )
        return self.async_create_entry(
            title=f"{NAME} ({PROFILE_LABELS[self._profile]})",
            data={CONF_PROFILE: self._profile, **self._entities},
            options=user_input,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MediaApplyOptionsFlow()


class MediaApplyOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_show_menu(step_id="init", menu_options=["entities", "options"])

    async def async_step_entities(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        profile = self.config_entry.data.get(CONF_PROFILE, DEFAULT_PROFILE)
        if user_input is not None:
            overrides = _entity_overrides(profile, user_input)
            new_data = {
                k: v for k, v in self.config_entry.data.items()
                if k not in ENTITY_SLOT_KEYS
            }
            new_data.update(overrides)
            new_data[CONF_PROFILE] = profile
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        return self.async_show_form(
            step_id="entities",
            data_schema=_entities_schema(_override_or_map(profile, self.config_entry.data)),
        )

    async def async_step_options(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="options", data_schema=_options_schema(self.config_entry.options),
        )
