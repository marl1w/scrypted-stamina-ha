"""Config, options and destination flows for Scrypted Stamina.

* **Setup** asks for the plugin URL and its token, and proves both with `/v1/health`.
* **Options**: one page of global settings (what is compared with what, how much is
  flagged), then one page per camera — linked entities pre-filled from the camera device's
  area, and the follow-up window.
* **Destinations** are subentries ("Add destination"), chosen from the storage integrations
  already set up, as in Reolink Stamina. Each has a folder, a quota, a retention, and a cap on
  the pending storage the follow-up window may hold.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.loader import async_get_integration
import voluptuous as vol

from .api import (
    CertificateChangedError,
    InsecureUrlError,
    StaminaApiError,
    StaminaAuthError,
    StaminaClient,
    async_probe_fingerprint,
    check_url,
    normalise_url,
)
from .const import (
    BASE_CLASSES,
    CONF_CAMERAS,
    CONF_DESTINATION_ENTRY,
    CONF_FINGERPRINT,
    CONF_FOLDER,
    CONF_FOLLOW_UP_CAP_GB,
    CONF_FOLLOW_UP_MINUTES,
    CONF_HISTORY_SIZE_MB,
    CONF_LINKED,
    CONF_ORPHAN_RETENTION_DAYS,
    CONF_QUOTA_GB,
    CONF_RECORDER_RETENTION_DAYS,
    CONF_RELEVANCE_SCOPE,
    CONF_RELEVANCE_SENSITIVITY,
    CONF_RETENTION_DAYS,
    CONF_TOKEN,
    CONF_UNVERIFIED_SCORE,
    CONF_UNVERIFIED_SECONDS,
    CONF_UPLOAD_CLASSES,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_FOLDER,
    DEFAULT_FOLLOW_UP_CAP_GB,
    DEFAULT_FOLLOW_UP_MINUTES,
    DEFAULT_HISTORY_SIZE_MB,
    DEFAULT_ORPHAN_RETENTION_DAYS,
    DEFAULT_QUOTA_GB,
    DEFAULT_RECORDER_RETENTION_DAYS,
    DEFAULT_RELEVANCE_SCOPE,
    DEFAULT_RELEVANCE_SENSITIVITY,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_UNVERIFIED_SCORE,
    DEFAULT_UNVERIFIED_SECONDS,
    DEFAULT_UPLOAD_CLASSES,
    DEFAULT_URL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    LEGACY_UNUSUAL_OVERRIDES,
    RELEVANCE_SCOPES,
    RELEVANCE_SENSITIVITY_FLOORS,
    SUBENTRY_TYPE_DESTINATION,
    TITLE,
)
from .destinations import DESTINATION_DOMAINS

CONF_ACCEPT = "accept_certificate"


def needs_pin(data: Mapping[str, Any]) -> bool:
    """Whether a URL's certificate is pinned: https without CA verification."""
    return urlsplit(str(data.get(CONF_URL, ""))).scheme == "https" and not data.get(
        CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL
    )


async def _async_validate(hass, data: Mapping[str, Any], fingerprint: str | None) -> dict[str, str]:
    """Call `/v1/health` with the URL, the token and exactly this pin; return form errors."""
    try:
        check_url(data[CONF_URL])
        client = StaminaClient(
            hass,
            data[CONF_URL],
            data[CONF_TOKEN],
            verify_ssl=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            fingerprint=fingerprint,
        )
    except InsecureUrlError:
        return {"base": "insecure_url"}
    try:
        await client.async_health()
    except CertificateChangedError:
        return {"base": "certificate_changed"}
    except StaminaAuthError:
        return {"base": "invalid_auth"}
    except StaminaApiError:
        return {"base": "cannot_connect"}
    return {}


def _accept_schema() -> vol.Schema:
    return vol.Schema({vol.Required(CONF_ACCEPT, default=False): bool})


def _connection_schema(current: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=current.get(CONF_URL, DEFAULT_URL)): str,
            vol.Required(CONF_TOKEN, default=current.get(CONF_TOKEN, "")): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_VERIFY_SSL, default=current.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
            ): bool,
        }
    )


class StaminaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect to the Stamina plugin.

    Plain http is offered by default, to the same host: Home Assistant and Scrypted usually
    share one, and loopback needs no certificate. For https to another host, Scrypted's
    certificate is self-signed, so it is shown and must be accepted: exactly the
    fingerprint shown is pinned, and a different certificate at submit time is refused.
    """

    VERSION = 1

    def __init__(self) -> None:
        """Hold the connection details while the certificate is confirmed."""
        self._pending: dict[str, Any] = {}
        self._shown: str | None = None
        self._finish = "create"

    async def _async_connection(
        self, user_input: dict[str, Any], step_id: str, current
    ) -> ConfigFlowResult:
        """Handle the URL-and-token form of setup and reconfigure."""
        errors: dict[str, str] = {}
        data = {**user_input, CONF_URL: normalise_url(user_input[CONF_URL])}
        try:
            check_url(data[CONF_URL])
        except InsecureUrlError:
            errors = {"base": "insecure_url"}
        if not errors and needs_pin(data):
            try:
                self._shown = await async_probe_fingerprint(self.hass, data[CONF_URL])
            except StaminaApiError:
                errors = {"base": "cannot_connect"}
            if not errors:
                self._pending = data
                return await self.async_step_certificate()
        if not errors:
            errors = await _async_validate(self.hass, data, None)
            if not errors:
                return self._done({**data, CONF_FINGERPRINT: None})
        return self.async_show_form(
            step_id=step_id, data_schema=_connection_schema(data or current), errors=errors
        )

    def _done(self, data: dict[str, Any]) -> ConfigFlowResult:
        if self._finish == "reconfigure":
            return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data=data)
        return self.async_create_entry(title=TITLE, data=data)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the plugin URL and token."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return await self._async_connection(user_input, "user", {})
        return self.async_show_form(step_id="user", data_schema=_connection_schema({}))

    async def async_step_certificate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the certificate's fingerprint and ask for it to be accepted."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_ACCEPT):
                errors = {"base": "not_accepted"}
            else:
                try:
                    presented = await async_probe_fingerprint(self.hass, self._pending[CONF_URL])
                except StaminaApiError:
                    presented = None
                if presented != self._shown:
                    # Not what was shown: show the new one instead of pinning it unseen.
                    self._shown = presented
                    errors = {"base": "certificate_changed"}
                else:
                    errors = await _async_validate(self.hass, self._pending, self._shown)
                    if not errors:
                        return self._done({**self._pending, CONF_FINGERPRINT: self._shown})
        return self.async_show_form(
            step_id="certificate",
            data_schema=_accept_schema(),
            description_placeholders={"fingerprint": self._shown or "-"},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Handle the plugin refusing the token, or presenting another certificate."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the token; and, if the certificate changed, for accepting the new one."""
        entry = self._get_reauth_entry()
        pinned = entry.data.get(CONF_FINGERPRINT)
        errors: dict[str, str] = {}
        if user_input is not None and needs_pin(entry.data):
            changed = self._shown != pinned
            try:
                presented = await async_probe_fingerprint(self.hass, entry.data[CONF_URL])
            except StaminaApiError:
                presented = None
            if changed and not user_input.get(CONF_ACCEPT):
                errors = {"base": "not_accepted"}
            elif presented != self._shown:
                self._shown = presented
                errors = {"base": "certificate_changed"}
        if user_input is not None and not errors:
            data = {**entry.data, CONF_TOKEN: user_input[CONF_TOKEN]}
            pin = self._shown if needs_pin(entry.data) else None
            errors = await _async_validate(self.hass, data, pin)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data={**data, CONF_FINGERPRINT: pin}
                )
        if user_input is None and needs_pin(entry.data):
            try:
                self._shown = await async_probe_fingerprint(self.hass, entry.data[CONF_URL])
            except StaminaApiError:
                self._shown = None
        changed = needs_pin(entry.data) and self._shown != pinned
        fields: dict[Any, Any] = {
            vol.Required(CONF_TOKEN): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            )
        }
        if changed:
            fields[vol.Required(CONF_ACCEPT, default=False)] = bool
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={
                "fingerprint": self._shown or "-",
                "changed": "yes" if changed else "no",
            },
            data_schema=vol.Schema(fields),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the URL or the token."""
        entry = self._get_reconfigure_entry()
        self._finish = "reconfigure"
        if user_input is not None:
            return await self._async_connection(user_input, "reconfigure", entry.data)
        return self.async_show_form(
            step_id="reconfigure", data_schema=_connection_schema(entry.data)
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> StaminaOptionsFlow:
        """Return the options flow."""
        return StaminaOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Destinations are subentries."""
        return {SUBENTRY_TYPE_DESTINATION: DestinationSubentryFlow}


class StaminaOptionsFlow(OptionsFlow):
    """Global settings, then one page per camera."""

    def __init__(self) -> None:
        """Hold the answers until the last page."""
        self._pending: dict[str, Any] = {}
        self._cameras: list[tuple[str, str, str | None]] = []
        self._at = 0

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask the global settings."""
        options = self.config_entry.options
        if user_input is not None:
            self._pending = {**options, **user_input}
            self._pending.pop(LEGACY_UNUSUAL_OVERRIDES, None)
            self._pending[CONF_CAMERAS] = dict(options.get(CONF_CAMERAS) or {})
            runtime = getattr(self.config_entry, "runtime_data", None)
            if runtime is not None:
                self._cameras = [
                    (camera, status.device.name, status.device.device_id)
                    for camera, status in sorted(
                        runtime.manager.cameras.items(), key=lambda item: item[1].device.name
                    )
                ]
            return await self.async_step_camera()

        return self.async_show_form(
            step_id="init",
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_RELEVANCE_SCOPE,
                        default=options.get(CONF_RELEVANCE_SCOPE, DEFAULT_RELEVANCE_SCOPE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(RELEVANCE_SCOPES),
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key="relevance_scope",
                        )
                    ),
                    vol.Required(
                        CONF_RELEVANCE_SENSITIVITY,
                        default=options.get(
                            CONF_RELEVANCE_SENSITIVITY, DEFAULT_RELEVANCE_SENSITIVITY
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(RELEVANCE_SENSITIVITY_FLOORS),
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key="relevance_sensitivity",
                        )
                    ),
                    vol.Required(
                        CONF_RECORDER_RETENTION_DAYS,
                        default=options.get(
                            CONF_RECORDER_RETENTION_DAYS, DEFAULT_RECORDER_RETENTION_DAYS
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=3650,
                            step=1,
                            unit_of_measurement="d",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_HISTORY_SIZE_MB,
                        default=options.get(CONF_HISTORY_SIZE_MB, DEFAULT_HISTORY_SIZE_MB),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=20,
                            max=10000,
                            step=10,
                            unit_of_measurement="MB",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_UPLOAD_CLASSES,
                        default=list(options.get(CONF_UPLOAD_CLASSES, DEFAULT_UPLOAD_CLASSES)),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._classes(),
                            multiple=True,
                            custom_value=True,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_UNVERIFIED_SECONDS,
                        default=options.get(CONF_UNVERIFIED_SECONDS, DEFAULT_UNVERIFIED_SECONDS),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=60,
                            step=0.5,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_UNVERIFIED_SCORE,
                        default=options.get(CONF_UNVERIFIED_SCORE, DEFAULT_UNVERIFIED_SCORE),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=1, step=0.05, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                }
            ),
        )

    def _classes(self) -> list[str]:
        """Return the classes to choose from: the usual ones, the cameras' own, the chosen."""
        found = set(BASE_CLASSES)
        found.update(self.config_entry.options.get(CONF_UPLOAD_CLASSES, DEFAULT_UPLOAD_CLASSES))
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is not None:
            for status in runtime.manager.cameras.values():
                reported = status.info.capabilities.get("classes")
                if isinstance(reported, list):
                    found.update(str(cls) for cls in reported if isinstance(cls, str))
        return sorted(found)

    async def async_step_camera(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask one camera's linked entities and follow-up window."""
        if user_input is not None:
            camera, _, _ = self._cameras[self._at]
            self._pending[CONF_CAMERAS][camera] = {
                CONF_LINKED: list(user_input.get(CONF_LINKED) or []),
                CONF_FOLLOW_UP_MINUTES: float(
                    user_input.get(CONF_FOLLOW_UP_MINUTES, DEFAULT_FOLLOW_UP_MINUTES)
                ),
            }
            self._at += 1
        if self._at >= len(self._cameras):
            return self.async_create_entry(data=self._pending)

        camera, name, _device_id = self._cameras[self._at]
        current = self._pending[CONF_CAMERAS].get(camera)
        # A camera starts with nothing linked: the user picks what matters, nothing is guessed.
        linked = list((current or {}).get(CONF_LINKED) or [])
        minutes = (current or {}).get(CONF_FOLLOW_UP_MINUTES, DEFAULT_FOLLOW_UP_MINUTES)
        return self.async_show_form(
            step_id="camera",
            last_step=self._at == len(self._cameras) - 1,
            description_placeholders={"camera": name},
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_LINKED, default=linked): selector.EntitySelector(
                        selector.EntitySelectorConfig(multiple=True)
                    ),
                    vol.Required(CONF_FOLLOW_UP_MINUTES, default=minutes): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=60,
                            step=1,
                            unit_of_measurement="min",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )


class DestinationSubentryFlow(ConfigSubentryFlow):
    """Add or change one destination."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose where events go, and how much room they may take."""
        used = {
            subentry.data.get(CONF_DESTINATION_ENTRY)
            for subentry in self._get_entry().subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_DESTINATION
        }
        options = [item for item in await self._async_destinations() if item["value"] not in used]
        if not options:
            return self.async_abort(reason="no_destination")
        if user_input is not None:
            label = next(
                item["label"]
                for item in options
                if item["value"] == user_input[CONF_DESTINATION_ENTRY]
            )
            return self.async_create_entry(
                title=label, data=user_input, unique_id=user_input[CONF_DESTINATION_ENTRY]
            )
        return self.async_show_form(
            step_id="user", data_schema=self._schema({}, options, choose=True)
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change a destination's folder, quota, retention or follow-up cap."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(self._get_entry(), subentry, data_updates=user_input)
        return self.async_show_form(
            step_id="reconfigure", data_schema=self._schema(subentry.data, [], choose=False)
        )

    async def _async_destinations(self) -> list[selector.SelectOptionDict]:
        """Return every account and server events could go to, across all providers."""
        options: list[selector.SelectOptionDict] = []
        for domain in DESTINATION_DOMAINS:
            entries = self.hass.config_entries.async_loaded_entries(domain)
            if not entries:
                continue
            integration = await async_get_integration(self.hass, domain)
            for entry in entries:
                options.append(
                    selector.SelectOptionDict(
                        value=entry.entry_id, label=f"{integration.name} — {entry.title}"
                    )
                )
        return options

    @staticmethod
    def _schema(
        current: Mapping[str, Any], choices: list[selector.SelectOptionDict], *, choose: bool
    ) -> vol.Schema:
        fields: dict[Any, Any] = {}
        if choose:
            fields[vol.Required(CONF_DESTINATION_ENTRY, default=choices[0]["value"])] = (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=choices, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                )
            )

        def number(high: float, step: float, unit: str) -> selector.NumberSelector:
            return selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=high,
                    step=step,
                    unit_of_measurement=unit,
                    mode=selector.NumberSelectorMode.BOX,
                )
            )

        return vol.Schema(
            {
                **fields,
                vol.Required(
                    CONF_FOLDER, default=current.get(CONF_FOLDER, DEFAULT_FOLDER)
                ): selector.TextSelector(),
                vol.Required(
                    CONF_QUOTA_GB, default=current.get(CONF_QUOTA_GB, DEFAULT_QUOTA_GB)
                ): number(10000, 1, "GB"),
                vol.Required(
                    CONF_RETENTION_DAYS,
                    default=current.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS),
                ): number(3650, 1, "d"),
                vol.Required(
                    CONF_ORPHAN_RETENTION_DAYS,
                    default=current.get(CONF_ORPHAN_RETENTION_DAYS, DEFAULT_ORPHAN_RETENTION_DAYS),
                ): number(3650, 1, "d"),
                vol.Required(
                    CONF_FOLLOW_UP_CAP_GB,
                    default=current.get(CONF_FOLLOW_UP_CAP_GB, DEFAULT_FOLLOW_UP_CAP_GB),
                ): number(1000, 0.1, "GB"),
            }
        )
