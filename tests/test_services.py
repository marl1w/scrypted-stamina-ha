"""`scrypted_stamina.delete_copies`: a finished event's videos go from every destination."""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError, Unauthorized
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scrypted_stamina.const import DOMAIN
from custom_components.scrypted_stamina.decider import EventState
from custom_components.scrypted_stamina.services import async_register_services

from .conftest import FakeDestination
from .test_commit import EVENT, PENDING, VIDEO, copy_of, index, make_record, prepared

__all__ = ["index"]


async def loaded(hass, index, fakes):
    uploader = await prepared(hass, index, fakes)
    record = make_record()
    await uploader.async_commit(record)
    record.state = EventState.COMMITTED
    record.finalized = True
    await index.async_save(record)
    entry = MockConfigEntry(domain=DOMAIN, title="Scrypted Stamina", state=ConfigEntryState.LOADED)
    entry.add_to_hass(hass)
    manager = SimpleNamespace(records={}, uploader=uploader)
    entry.runtime_data = SimpleNamespace(index=index, manager=manager)
    async_register_services(hass)
    return manager, record


async def test_delete_copies_clears_every_destination_and_keeps_the_event(hass, index) -> None:
    a, b = FakeDestination(), FakeDestination()
    await loaded(hass, index, {"a": a, "b": b})
    assert a.files.get(VIDEO) and b.files.get(VIDEO)
    result = await hass.services.async_call(
        DOMAIN, "delete_copies", {"event_id": EVENT}, blocking=True, return_response=True
    )
    assert len(result["deleted"][EVENT]) == 2
    for fake in (a, b):
        assert not fake.videos("f/events/")
        assert not any(name.startswith(PENDING) for name in fake.files)
    assert (await copy_of(index, "a")).deleted and (await copy_of(index, "b")).deleted
    assert await index.async_get(EVENT) is not None  # the history keeps it
    again = await hass.services.async_call(
        DOMAIN, "delete_copies", {"event_id": [EVENT]}, blocking=True, return_response=True
    )
    assert again["deleted"][EVENT] == []  # nothing left, nothing done


async def test_an_event_still_being_decided_is_refused(hass, index) -> None:
    fake = FakeDestination()
    manager, record = await loaded(hass, index, {"a": fake})
    record.finalized = False
    manager.records[EVENT] = record
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "delete_copies", {"event_id": EVENT}, blocking=True)
    assert fake.videos("f/events/")


async def test_an_unknown_event_is_refused(hass, index) -> None:
    await loaded(hass, index, {"a": FakeDestination()})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "delete_copies", {"event_id": "ev_nope"}, blocking=True
        )


async def test_delete_copies_is_admin_only(hass, index, hass_read_only_user) -> None:
    fake = FakeDestination()
    await loaded(hass, index, {"a": fake})
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "delete_copies",
            {"event_id": EVENT},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    assert fake.videos("f/events/")
