"""Folder layout (architecture §3.5): area folders, camera slugs, event folder names."""

from __future__ import annotations

import datetime as dt

from homeassistant.helpers import area_registry as ar, device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scrypted_stamina.devices import resolve_camera
from custom_components.scrypted_stamina.naming import (
    area_slug,
    camera_slug,
    event_file_name,
    event_file_path,
    event_folder_name,
    event_path,
    root_folder,
    safe_part,
    segment_name,
    video_part_path,
)


def test_area_is_slugified_or_no_area() -> None:
    assert area_slug("Front Garden") == "front-garden"
    assert area_slug("Front Door / Stairs") == "front-door-stairs"
    assert area_slug(None) == "no-area"
    assert area_slug("") == "no-area"
    assert area_slug("!!!") == "no-area"


def test_camera_name_is_slugified() -> None:
    assert camera_slug("Front Door") == "front-door"
    assert camera_slug("", fallback="38") == "38"


def test_event_folder_name() -> None:
    when = dt.datetime(2026, 10, 31, 21, 5, 9)
    name = event_folder_name(when, "Front Door", "ev_q2Jx8sK4mZb1VtR6yN0aLw")
    assert name == "261031_210509_front-door_ev_q2Jx8sK4mZb1VtR6yN0aLw"


def test_event_paths_per_kind() -> None:
    name = "261031_210509_front-door_ev_1"
    assert event_path("scrypted-stamina", "pending", "garden", name) == (
        "scrypted-stamina/pending/garden/261031_210509_front-door_ev_1"
    )
    assert event_path("home/cams", "unusual-unconfirmed", "no-area", name).startswith(
        "home/cams/unusual-unconfirmed/no-area/"
    )


def test_configured_folder_cannot_escape() -> None:
    assert root_folder("../../etc") == "etc"
    assert root_folder("a/../b") == "a/b"
    assert root_folder("") == "scrypted-stamina"
    assert safe_part("a:b*c") == "a-b-c"


def test_segment_names_sort_in_play_order() -> None:
    assert segment_name(0) == "00000.m4s"
    assert sorted(segment_name(n) for n in (10, 2, 0)) == ["00000.m4s", "00002.m4s", "00010.m4s"]


async def test_area_comes_from_the_camera_device(hass) -> None:
    """The camera device's HA area decides the folder, whichever device it is."""
    owner = MockConfigEntry(domain="scrypted_mqtt")
    owner.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Back Yard")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("scrypted_mqtt", "scrypted:38")},
        name="Back Gate",
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)

    camera = resolve_camera(hass, "38", "Camera 38")
    assert camera.shared
    assert camera.area_slug == "back-yard"
    assert camera.slug == "back-gate"

    alone = resolve_camera(hass, "99", "Porch")
    assert not alone.shared
    assert alone.area_slug == "no-area"
    assert alone.slug == "porch"


def test_a_committed_event_is_one_video_named_area_camera_time() -> None:
    when = dt.datetime(2026, 10, 31, 21, 13, 49)
    name = event_file_name(when, "front-garden", "Front Door")
    assert name == "front-garden-front-door-261031_211349.mp4"
    assert event_file_path("scrypted-stamina", "events", name) == (
        "scrypted-stamina/events/front-garden-front-door-261031_211349.mp4"
    )
    # Whatever the pieces, a name never reaches outside its folder.
    assert event_file_name(when, "../odd", "a/b") == "odd-a-b-261031_211349.mp4"
    assert "/" not in event_file_path("x", "events", "../../etc").removeprefix("x/events/")


def test_what_a_video_receives_later_is_a_file_of_its_own() -> None:
    video = "scrypted-stamina/events/garden-front-door-261031_211349.mp4"
    assert video_part_path(video, 1) == video
    assert video_part_path(video, 2) == (
        "scrypted-stamina/events/garden-front-door-261031_211349-part2.mp4"
    )
