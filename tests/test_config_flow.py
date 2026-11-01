"""Config flow (URL and token, checked by /v1/health), options, and destination subentries."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scrypted_stamina.const import (
    CONF_CAMERAS,
    CONF_DESTINATION_ENTRY,
    CONF_FOLDER,
    CONF_FOLLOW_UP_CAP_GB,
    CONF_FOLLOW_UP_MINUTES,
    CONF_LINKED,
    CONF_LOOK_BACK_MINUTES,
    CONF_QUOTA_GB,
    CONF_RELEVANCE_SCOPE,
    CONF_RETENTION_DAYS,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_URL,
    DOMAIN,
    SUBENTRY_TYPE_DESTINATION,
)

from .conftest import TOKEN


async def test_user_flow_checks_health_and_creates_the_entry(hass, stamina) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    schema = {
        str(key): key.default() for key in result["data_schema"].schema if callable(key.default)
    }
    assert schema[CONF_URL] == DEFAULT_URL

    with patch("custom_components.scrypted_stamina.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: stamina.url + "/", CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: False},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == stamina.url  # normalised


async def test_user_flow_reports_a_wrong_token(hass, stamina) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: stamina.url, CONF_TOKEN: "wrong", CONF_VERIFY_SSL: False}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_reports_an_unreachable_plugin(hass, stamina) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    stamina.health_status = 500
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: stamina.url, CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: False}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_only_one_entry(hass, stamina) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT


async def test_a_refused_token_starts_reauth(hass, stamina) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={CONF_URL: stamina.url, CONF_TOKEN: "old"}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert flows and flows[0]["context"]["source"] == "reauth"

    with patch("custom_components.scrypted_stamina.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"], {CONF_TOKEN: TOKEN}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == TOKEN


async def test_destination_subentry_from_a_loaded_storage_integration(hass, stamina, setup) -> None:
    entry = await setup(destinations=0)
    storage = MockConfigEntry(
        domain="sftp_storage", title="Backup box", state=ConfigEntryState.LOADED
    )
    storage.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_DESTINATION), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    defaults = {
        str(key): key.default() for key in result["data_schema"].schema if callable(key.default)
    }
    assert defaults[CONF_FOLDER] == "scrypted-stamina"
    assert defaults[CONF_QUOTA_GB] == 15
    assert defaults[CONF_DESTINATION_ENTRY] == storage.entry_id

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_DESTINATION_ENTRY: storage.entry_id,
            CONF_FOLDER: "cams",
            CONF_QUOTA_GB: 20,
            CONF_RETENTION_DAYS: 30,
            CONF_FOLLOW_UP_CAP_GB: 1,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.get_subentries_of_type(SUBENTRY_TYPE_DESTINATION)
    assert subentry.title == "SFTP Storage — Backup box"
    assert subentry.data[CONF_QUOTA_GB] == 20

    # The same storage cannot be added twice.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_DESTINATION), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_destination"
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    # Only pretended to be loaded: teardown must not try to unload the real integration
    # (whose library the test environment does not have).
    storage.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_options_have_a_page_per_camera_starting_empty(hass, stamina, setup) -> None:
    mqtt = MockConfigEntry(domain="scrypted_mqtt")
    mqtt.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Porch")
    devices = dr.async_get(hass)
    camera = devices.async_get_or_create(
        config_entry_id=mqtt.entry_id,
        identifiers={("scrypted_mqtt", "scrypted:38")},
        name="Front door",
    )
    devices.async_update_device(camera.id, area_id=area.id)
    other = MockConfigEntry(domain="zha")
    other.add_to_hass(hass)
    contact = devices.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("zha", "contact")}, name="Door contact"
    )
    devices.async_update_device(contact.id, area_id=area.id)
    entities = er.async_get(hass)
    entities.async_get_or_create(
        "binary_sensor", "zha", "contact", device_id=contact.id, original_device_class="door"
    )
    entities.async_get_or_create(
        "binary_sensor", "zha", "battery", device_id=contact.id, original_device_class="battery"
    )
    door = entities.async_get("binary_sensor.zha_contact") or next(
        e for e in entities.entities.values() if e.unique_id == "contact"
    )

    entry = await setup()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_RELEVANCE_SCOPE: "area"}
    )
    assert result["step_id"] == "camera"
    assert result["description_placeholders"] == {"camera": "Front door"}
    defaults = {
        str(key): key.default() for key in result["data_schema"].schema if callable(key.default)
    }
    assert defaults[CONF_LINKED] == []  # nothing is guessed, even with a door in the area
    assert defaults[CONF_FOLLOW_UP_MINUTES] == 10
    assert defaults[CONF_LOOK_BACK_MINUTES] == 2

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LINKED: [door.entity_id], CONF_FOLLOW_UP_MINUTES: 5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_RELEVANCE_SCOPE] == "area"
    assert entry.options[CONF_CAMERAS]["38"] == {
        CONF_LINKED: [door.entity_id],
        CONF_LOOK_BACK_MINUTES: 2.0,
        CONF_FOLLOW_UP_MINUTES: 5.0,
    }
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_public_path_is_used_with_or_without_it(hass, stamina) -> None:
    """`docs/api.md`: routes live under `/public/`, the one the plugin's token guards."""
    from custom_components.scrypted_stamina.api import api_base

    assert api_base("https://192.0.2.5:10443/endpoint/@marl1w/scrypted-stamina") == (
        "https://192.0.2.5:10443/endpoint/@marl1w/scrypted-stamina/public"
    )
    assert api_base(DEFAULT_URL + "/") == DEFAULT_URL
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch("custom_components.scrypted_stamina.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: stamina.url + "/public", CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: False},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
