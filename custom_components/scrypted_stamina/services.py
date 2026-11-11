"""Services: `scrypted_stamina.delete_copies` (admin only).

Deletes the videos of finished events from every destination, as the quota's eviction does:
the event stays in the index, with its copies marked deleted, and the model keeps learning
from it. For a kept event that turned out to be a false alarm. An event still being decided
is refused: its footage may still be uploading.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
import voluptuous as vol

from .const import DOMAIN
from .destinations import DestinationError

if TYPE_CHECKING:
    from . import StaminaData

SERVICE_DELETE_COPIES = "delete_copies"
ATTR_EVENT_ID = "event_id"
_REGISTERED = f"{DOMAIN}_services"

_EVENT_ID = vol.All(str, vol.Length(min=1, max=96))
SCHEMA = vol.Schema({vol.Required(ATTR_EVENT_ID): vol.All(cv.ensure_list, [_EVENT_ID])})


def _data(hass: HomeAssistant) -> StaminaData:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")


async def _async_delete_copies(call: ServiceCall) -> ServiceResponse:
    data = _data(call.hass)
    ids: list[str] = call.data[ATTR_EVENT_ID]
    for event_id in ids:
        live = data.manager.records.get(event_id)
        if live is not None and not live.finalized:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="event_undecided",
                translation_placeholders={"event_id": event_id},
            )
        if await data.index.async_get(event_id) is None and live is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="event_not_found",
                translation_placeholders={"event_id": event_id},
            )
    deleted: dict[str, Any] = {}
    for event_id in ids:
        try:
            deleted[event_id] = await data.manager.uploader.async_delete_copies(event_id)
        except DestinationError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="delete_failed",
                translation_placeholders={"event_id": event_id, "error": str(err)},
            ) from err
    return {"deleted": deleted}


def async_register_services(hass: HomeAssistant) -> None:
    """Register the services once per Home Assistant run."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_DELETE_COPIES,
        _async_delete_copies,
        schema=SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
