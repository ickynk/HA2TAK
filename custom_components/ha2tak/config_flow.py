"""Config and options flows for the HA2TAK integration."""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CALLSIGN,
    CONF_COT_TYPE,
    CONF_ENTITIES,
    CONF_INTERVAL,
    CONF_MODE,
    CONF_PROTOCOL,
    CONF_REMARKS,
    CONF_SEND_ON_UPDATE,
    CONF_STALE,
    CONF_TEAM_COLOR,
    CONF_TEAM_ROLE,
    CONF_TLS_CA_FILE,
    CONF_TLS_CERT_FILE,
    CONF_TLS_CHECK_HOSTNAME,
    CONF_TLS_KEY_FILE,
    CONF_TLS_P12_FILE,
    CONF_TLS_P12_PASSWORD,
    CONF_TLS_VERIFY,
    DEFAULT_INTERVAL,
    DEFAULT_MARKER_TYPE,
    DEFAULT_PORT,
    DEFAULT_STALE,
    DEFAULT_TEAM_COLOR,
    DEFAULT_TEAM_ROLE,
    DOMAIN,
    MARKER_TYPES,
    MAX_INTERVAL,
    MIN_INTERVAL,
    MODE_CONTACT,
    MODE_MARKER,
    MODES,
    PROTOCOLS,
    TEAM_COLORS,
    TEAM_ROLES,
    TRACKABLE_DOMAINS,
)
from .tak import TakConfigError, TakConnection

_LOGGER = logging.getLogger(__name__)


def _connection_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=65535, step=1, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_PROTOCOL, default=defaults.get(CONF_PROTOCOL, "tcp")
            ): SelectSelector(
                SelectSelectorConfig(
                    options=PROTOCOLS,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="protocol",
                )
            ),
            vol.Optional(
                CONF_TLS_CA_FILE, default=defaults.get(CONF_TLS_CA_FILE, "")
            ): str,
            vol.Optional(
                CONF_TLS_CERT_FILE, default=defaults.get(CONF_TLS_CERT_FILE, "")
            ): str,
            vol.Optional(
                CONF_TLS_KEY_FILE, default=defaults.get(CONF_TLS_KEY_FILE, "")
            ): str,
            vol.Optional(
                CONF_TLS_P12_FILE, default=defaults.get(CONF_TLS_P12_FILE, "")
            ): str,
            vol.Optional(
                CONF_TLS_P12_PASSWORD,
                default=defaults.get(CONF_TLS_P12_PASSWORD, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Required(
                CONF_TLS_VERIFY, default=defaults.get(CONF_TLS_VERIFY, True)
            ): BooleanSelector(),
            vol.Required(
                CONF_TLS_CHECK_HOSTNAME,
                default=defaults.get(CONF_TLS_CHECK_HOSTNAME, False),
            ): BooleanSelector(),
        }
    )


class Ha2TakConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up (and reconfigure) the connection to a TAK server."""

    VERSION = 1

    async def _async_validate(
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Try to actually connect (incl. TLS handshake). Returns
        (errors, description_placeholders)."""
        user_input[CONF_PORT] = int(user_input[CONF_PORT])
        connection = TakConnection(self.hass, user_input)
        try:
            await connection.async_connect()
        except TakConfigError as exc:
            return {"base": "invalid_tls_config"}, {"error": str(exc)}
        except ssl.SSLCertVerificationError as exc:
            return {"base": "invalid_certificate"}, {"error": str(exc)}
        except (OSError, asyncio.TimeoutError, ssl.SSLError) as exc:
            _LOGGER.warning("Could not connect to TAK server: %s", exc)
            return {"base": "cannot_connect"}, {"error": str(exc)}
        finally:
            await connection.async_close()
        return {}, {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            errors, placeholders = await self._async_validate(user_input)
            if not errors:
                await self.async_set_unique_id(
                    f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"TAK server {user_input[CONF_HOST]}",
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(user_input or {}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            errors, placeholders = await self._async_validate(user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(user_input or dict(entry.data)),
            errors=errors,
            description_placeholders=placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> Ha2TakOptionsFlow:
        return Ha2TakOptionsFlow()


class Ha2TakOptionsFlow(OptionsFlow):
    """Manage reporting settings and the set of tracked entities."""

    def __init__(self) -> None:
        self._entity_id: str | None = None

    @property
    def _entities(self) -> dict[str, dict[str, Any]]:
        return self.config_entry.options.get(CONF_ENTITIES, {})

    def _save_options(self, **updates: Any) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        options.update(updates)
        return self.async_create_entry(data=options)

    def _save_entity(self, cfg: dict[str, Any]) -> ConfigFlowResult:
        assert self._entity_id is not None
        entities = dict(self._entities)
        entities[self._entity_id] = cfg
        return self._save_options(**{CONF_ENTITIES: entities})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        menu = ["add_entity", "settings"]
        if self._entities:
            menu[1:1] = ["edit_entity", "remove_entity"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self.config_entry.options
        if user_input is not None:
            return self._save_options(
                **{
                    CONF_INTERVAL: int(user_input[CONF_INTERVAL]),
                    CONF_STALE: int(user_input[CONF_STALE]),
                    CONF_SEND_ON_UPDATE: user_input[CONF_SEND_ON_UPDATE],
                }
            )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_INTERVAL,
                    default=options.get(CONF_INTERVAL, DEFAULT_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_INTERVAL,
                        max=MAX_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_STALE, default=options.get(CONF_STALE, DEFAULT_STALE)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=30,
                        max=MAX_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_SEND_ON_UPDATE,
                    default=options.get(CONF_SEND_ON_UPDATE, False),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_add_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = user_input["entity_id"]
            if entity_id in self._entities:
                errors["base"] = "already_tracked"
            else:
                self._entity_id = entity_id
                if user_input[CONF_MODE] == MODE_CONTACT:
                    return await self.async_step_contact()
                return await self.async_step_marker()
        schema = vol.Schema(
            {
                vol.Required("entity_id"): EntitySelector(
                    EntitySelectorConfig(domain=TRACKABLE_DOMAINS)
                ),
                vol.Required(CONF_MODE, default=MODE_CONTACT): SelectSelector(
                    SelectSelectorConfig(
                        options=MODES,
                        mode=SelectSelectorMode.LIST,
                        translation_key="mode",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="add_entity", data_schema=schema, errors=errors
        )

    async def async_step_edit_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._entity_id = user_input["entity_id"]
            return await self.async_step_mode()
        options = [
            SelectOptionDict(
                value=entity_id,
                label=f"{cfg.get(CONF_CALLSIGN, entity_id)} ({entity_id})",
            )
            for entity_id, cfg in self._entities.items()
        ]
        schema = vol.Schema(
            {
                vol.Required("entity_id"): SelectSelector(
                    SelectSelectorConfig(
                        options=options, mode=SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(step_id="edit_entity", data_schema=schema)

    async def async_step_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            if user_input[CONF_MODE] == MODE_CONTACT:
                return await self.async_step_contact()
            return await self.async_step_marker()
        current = self._entities.get(self._entity_id or "", {})
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MODE, default=current.get(CONF_MODE, MODE_CONTACT)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=MODES,
                        mode=SelectSelectorMode.LIST,
                        translation_key="mode",
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="mode",
            data_schema=schema,
            description_placeholders={"entity": self._entity_id or ""},
        )

    def _default_callsign(self, current: dict[str, Any]) -> str:
        if current.get(CONF_CALLSIGN):
            return current[CONF_CALLSIGN]
        assert self._entity_id is not None
        state = self.hass.states.get(self._entity_id)
        return state.name if state else self._entity_id

    async def async_step_contact(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._save_entity(
                {
                    CONF_MODE: MODE_CONTACT,
                    CONF_CALLSIGN: user_input[CONF_CALLSIGN].strip(),
                    CONF_TEAM_COLOR: user_input[CONF_TEAM_COLOR],
                    CONF_TEAM_ROLE: user_input[CONF_TEAM_ROLE],
                    CONF_REMARKS: user_input.get(CONF_REMARKS, "").strip(),
                }
            )
        current = self._entities.get(self._entity_id or "", {})
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CALLSIGN, default=self._default_callsign(current)
                ): str,
                vol.Required(
                    CONF_TEAM_COLOR,
                    default=current.get(CONF_TEAM_COLOR, DEFAULT_TEAM_COLOR),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=TEAM_COLORS, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Required(
                    CONF_TEAM_ROLE,
                    default=current.get(CONF_TEAM_ROLE, DEFAULT_TEAM_ROLE),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=TEAM_ROLES, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(
                    CONF_REMARKS, default=current.get(CONF_REMARKS, "")
                ): str,
            }
        )
        return self.async_show_form(
            step_id="contact",
            data_schema=schema,
            description_placeholders={"entity": self._entity_id or ""},
        )

    async def async_step_marker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._save_entity(
                {
                    CONF_MODE: MODE_MARKER,
                    CONF_CALLSIGN: user_input[CONF_CALLSIGN].strip(),
                    CONF_COT_TYPE: user_input[CONF_COT_TYPE],
                    CONF_REMARKS: user_input.get(CONF_REMARKS, "").strip(),
                }
            )
        current = self._entities.get(self._entity_id or "", {})
        marker_options = [
            SelectOptionDict(value=cot_type, label=label)
            for cot_type, label in MARKER_TYPES.items()
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CALLSIGN, default=self._default_callsign(current)
                ): str,
                vol.Required(
                    CONF_COT_TYPE,
                    default=current.get(CONF_COT_TYPE, DEFAULT_MARKER_TYPE),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=marker_options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(
                    CONF_REMARKS, default=current.get(CONF_REMARKS, "")
                ): str,
            }
        )
        return self.async_show_form(
            step_id="marker",
            data_schema=schema,
            description_placeholders={"entity": self._entity_id or ""},
        )

    async def async_step_remove_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            entities = {
                entity_id: cfg
                for entity_id, cfg in self._entities.items()
                if entity_id not in user_input["entity_ids"]
            }
            return self._save_options(**{CONF_ENTITIES: entities})
        options = [
            SelectOptionDict(
                value=entity_id,
                label=f"{cfg.get(CONF_CALLSIGN, entity_id)} ({entity_id})",
            )
            for entity_id, cfg in self._entities.items()
        ]
        schema = vol.Schema(
            {
                vol.Required("entity_ids", default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                        multiple=True,
                    )
                )
            }
        )
        return self.async_show_form(step_id="remove_entity", data_schema=schema)
