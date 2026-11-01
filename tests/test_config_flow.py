"""Config flow (URL and token, checked by /v1/health), options, destination and camera flows."""

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
    CONF_PLAYBACK_ORDER,
    CONF_QUOTA_GB,
    CONF_RELEVANCE_SCOPE,
    CONF_RETENTION_DAYS,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_URL,
    DOMAIN,
    SUBENTRY_TYPE_CAMERA,
    SUBENTRY_TYPE_DESTINATION,
)

from .conftest import TOKEN, make_entry


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
    assert subentry.title == "SFTP Storage"
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


def defaults_of(result) -> dict:
    return {
        str(key): key.default() for key in result["data_schema"].schema if callable(key.default)
    }


async def test_options_are_one_page_of_global_settings(hass, setup) -> None:
    entry = await setup()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert defaults_of(result)[CONF_PLAYBACK_ORDER] == "cloud_first"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_RELEVANCE_SCOPE: "area", CONF_PLAYBACK_ORDER: "recording_first"}
    )
    # No page per camera: those settings are in each camera's subentry.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_RELEVANCE_SCOPE] == "area"
    assert entry.options[CONF_PLAYBACK_ORDER] == "recording_first"
    assert CONF_CAMERAS not in entry.options
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_each_camera_is_a_subentry_with_its_own_settings(hass, stamina, setup) -> None:
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
    door = entities.async_get_or_create(
        "binary_sensor", "zha", "contact", device_id=contact.id, original_device_class="door"
    )

    entry = await setup()
    manager = entry.runtime_data.manager
    (subentry,) = entry.get_subentries_of_type(SUBENTRY_TYPE_CAMERA)
    assert (subentry.title, subentry.unique_id) == ("Front door", "38")
    # The camera's device and entities are filed under it.
    item = entities.async_get("event.porch_front_door_stamina")
    assert item.config_subentry_id == subentry.subentry_id
    assert devices.async_get(item.device_id).config_subentry_id == subentry.subentry_id

    # The integration adds cameras; the user cannot.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_CAMERA), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "managed"

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_CAMERA),
        context={"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    assert result["description_placeholders"] == {"camera": "Front door"}
    defaults = defaults_of(result)
    assert defaults[CONF_LINKED] == []  # nothing is guessed, even with a door in the area
    assert defaults[CONF_FOLLOW_UP_MINUTES] == 10
    assert defaults[CONF_LOOK_BACK_MINUTES] == 2
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_LINKED: [door.entity_id], CONF_FOLLOW_UP_MINUTES: 5}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.subentries[subentry.subentry_id].data) == {
        CONF_LINKED: [door.entity_id],
        CONF_LOOK_BACK_MINUTES: 2.0,
        CONF_FOLLOW_UP_MINUTES: 5.0,
    }
    await hass.async_block_till_done()
    # Applied at once, without restarting the integration.
    assert entry.runtime_data.manager is manager
    assert manager.linked("38") == [door.entity_id]
    assert manager.follow_up_seconds("38") == 300
    await hass.config_entries.async_unload(entry.entry_id)


async def test_per_camera_options_move_into_the_subentries(hass, stamina, setup) -> None:
    """An upgrade: the options' settings, entity ids and history all stay."""
    settings = {CONF_LINKED: ["binary_sensor.gate"], CONF_FOLLOW_UP_MINUTES: 7}
    entry = make_entry(hass, stamina, options={CONF_CAMERAS: {"38": settings}})
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "camera:38")}, name="Front door"
    )
    old = entities.async_get_or_create(
        "event",
        DOMAIN,
        f"{entry.entry_id}_38_stamina",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="front_door_stamina",
    )

    await setup(entry=entry)
    assert CONF_CAMERAS not in entry.options
    (subentry,) = entry.get_subentries_of_type(SUBENTRY_TYPE_CAMERA)
    assert dict(subentry.data) == settings
    assert entry.runtime_data.manager.linked("38") == ["binary_sensor.gate"]
    item = entities.async_get(old.entity_id)
    assert item.id == old.id and item.entity_id == "event.front_door_stamina"
    assert item.config_subentry_id == subentry.subentry_id
    assert devices.async_get(device.id).config_subentry_id == subentry.subentry_id
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_camera_leaving_scrypted_takes_its_subentry(hass, stamina, setup) -> None:
    stamina.cameras.append({**stamina.cameras[0], "id": "39", "name": "Gate"})
    entry = await setup()
    manager = entry.runtime_data.manager
    assert {s.unique_id for s in entry.get_subentries_of_type(SUBENTRY_TYPE_CAMERA)} == {"38", "39"}
    assert hass.states.get("event.gate_stamina") is not None

    gate = stamina.cameras.pop()
    await manager.async_refresh_cameras()
    await hass.async_block_till_done()
    assert {s.unique_id for s in entry.get_subentries_of_type(SUBENTRY_TYPE_CAMERA)} == {"38"}
    assert er.async_get(hass).async_get("event.gate_stamina") is None

    # Back again: a new subentry, with its entities.
    stamina.cameras.append(gate)
    await manager.async_refresh_cameras()
    await hass.async_block_till_done()
    assert {s.unique_id for s in entry.get_subentries_of_type(SUBENTRY_TYPE_CAMERA)} == {"38", "39"}
    assert er.async_get(hass).async_get("event.gate_stamina") is not None
    assert entry.runtime_data.manager is manager
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
