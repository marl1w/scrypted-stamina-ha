"""No token, password or address leaves through logs, diagnostics or attributes."""

from __future__ import annotations

import json

from custom_components.scrypted_stamina.diagnostics import async_get_config_entry_diagnostics
from custom_components.scrypted_stamina.redact import REDACTED, scrub_credentials

from .conftest import TOKEN


def test_literal_secrets_and_patterns_are_scrubbed() -> None:
    text = (
        "GET https://192.0.2.10/x?token=abc&sig=deadbeef failed: "
        "Authorization: Bearer s3cr3t-token user=admin&password=p&ss"
    )
    scrubbed = scrub_credentials(text, secrets=("s3cr3t-token",))
    assert "s3cr3t-token" not in scrubbed
    assert "abc" not in scrubbed and "deadbeef" not in scrubbed
    assert "p&ss" not in scrubbed and "admin" not in scrubbed
    assert scrubbed.count(REDACTED) >= 4


def test_short_secrets_are_not_used_literally() -> None:
    assert scrub_credentials("a b c", secrets=("a",)) == "a b c"


async def test_diagnostics_hide_the_token_and_the_host(hass, setup) -> None:
    entry = await setup()
    report = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(report, default=str)
    assert TOKEN not in dumped
    assert "127.0.0.1" not in dumped
    assert report["entry"]["url"].endswith("/endpoint/@marl1w/scrypted-stamina")
    assert report["link"]["connected"] is True
    await hass.config_entries.async_unload(entry.entry_id)
