"""Security review fixes: TLS pinning, redirects, content types, limits, scrubbing."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import ipaddress
import ssl

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.scrypted_stamina.api import (
    CertificateChangedError,
    InsecureUrlError,
    StaminaApiError,
    StaminaClient,
    async_probe_fingerprint,
    async_read_limited,
    check_url,
)
from custom_components.scrypted_stamina.const import (
    CONF_FINGERPRINT,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
)
from custom_components.scrypted_stamina.playback import _byte_range, rewrite_playlist, safe_type
from custom_components.scrypted_stamina.protocol import EventFacts
from custom_components.scrypted_stamina.uploader import Target

from .conftest import TOKEN, FakeDestination


def self_signed(tmp_path) -> tuple[ssl.SSLContext, str]:
    """Return a server SSL context for 127.0.0.1 and its certificate's SHA-256."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "scrypted")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_path, key_path)
    return context, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


@pytest.fixture
async def tls_stamina(stamina, aiohttp_server, tmp_path):
    """The fake plugin, served over HTTPS with a self-signed certificate."""
    context, fingerprint = self_signed(tmp_path)
    server = await aiohttp_server(stamina.app(), ssl=context)
    url = str(server.make_url("/endpoint/@marl1w/scrypted-stamina"))
    return url, fingerprint


# ------------------------------------------------------------------ 17. TLS


async def test_the_certificate_is_shown_accepted_and_pinned(hass, tls_stamina) -> None:
    from unittest.mock import patch

    url, fingerprint = tls_stamina
    assert await async_probe_fingerprint(hass, url) == fingerprint

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: url, CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: False}
    )
    assert result["step_id"] == "certificate"
    assert result["description_placeholders"] == {"fingerprint": fingerprint}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_certificate": False}
    )
    assert result["errors"] == {"base": "not_accepted"}
    with patch("custom_components.scrypted_stamina.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"accept_certificate": True}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_FINGERPRINT] == fingerprint

    pinned = StaminaClient(hass, url, TOKEN, fingerprint=fingerprint)
    assert (await pinned.async_health())["ok"]
    impostor = StaminaClient(hass, url, TOKEN, fingerprint="00" * 32)
    with pytest.raises(CertificateChangedError):
        await impostor.async_health()
    # Never a silent unverified connection: https without verification needs a pin.
    with pytest.raises(InsecureUrlError):
        StaminaClient(hass, url, TOKEN)


async def test_a_different_certificate_at_submit_is_not_pinned(hass, tls_stamina) -> None:
    url, _ = tls_stamina
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: url, CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: False}
    )
    from unittest.mock import AsyncMock, patch

    with patch(
        "custom_components.scrypted_stamina.config_flow.async_probe_fingerprint",
        AsyncMock(return_value="ab" * 32),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"accept_certificate": True}
        )
    assert result["errors"] == {"base": "certificate_changed"}
    assert result["description_placeholders"] == {"fingerprint": "ab" * 32}


async def test_reauth_with_a_changed_certificate_needs_acceptance(hass, tls_stamina) -> None:
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    url, fingerprint = tls_stamina
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_URL: url, CONF_TOKEN: TOKEN, CONF_FINGERPRINT: "00" * 32},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["description_placeholders"]["changed"] == "yes"
    assert result["description_placeholders"]["fingerprint"] == fingerprint
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: TOKEN, "accept_certificate": False}
    )
    assert result["errors"] == {"base": "not_accepted"}
    with patch("custom_components.scrypted_stamina.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: TOKEN, "accept_certificate": True}
        )
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_FINGERPRINT] == fingerprint


async def test_reauth_keeps_an_unchanged_pin(hass, tls_stamina) -> None:
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    url, fingerprint = tls_stamina
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_URL: url, CONF_TOKEN: "old", CONF_FINGERPRINT: fingerprint},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["description_placeholders"]["changed"] == "no"
    with patch("custom_components.scrypted_stamina.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: TOKEN}
        )
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_FINGERPRINT] == fingerprint and entry.data[CONF_TOKEN] == TOKEN


async def test_plain_http_is_refused_except_to_this_machine(hass) -> None:
    check_url("http://127.0.0.1:11080/endpoint/@marl1w/scrypted-stamina/public")
    check_url("http://localhost:11080/x")
    check_url("https://192.0.2.10:10443/x")
    with pytest.raises(InsecureUrlError):
        check_url("http://192.0.2.10:11080/endpoint/@marl1w/scrypted-stamina/public")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "http://192.0.2.10:11080/x", CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: False},
    )
    assert result["errors"] == {"base": "insecure_url"}


# ------------------------------------------------------------------ 16. redirects


async def test_redirects_stay_on_the_plugin(hass, stamina) -> None:
    client = StaminaClient(hass, stamina.url, TOKEN)
    stamina.redirect_to = "http://192.0.2.1/steal"
    with pytest.raises(StaminaApiError, match="outside"):
        await client.async_open_raw("/v1/cameras/38/redirect.m3u8")
    stamina.redirect_to = "/somewhere-else/index.m3u8"
    with pytest.raises(StaminaApiError, match="outside"):
        await client.async_open_raw("/v1/cameras/38/redirect.m3u8")
    for sneaky in (
        "http://user:pw@127.0.0.1/endpoint/@marl1w/scrypted-stamina/public/v1/x",
        "/endpoint/@marl1w/scrypted-stamina/public/v1/cameras/38%2F..%2F..%2Fhealth",
        "/endpoint/@marl1w/scrypted-stamina/public/v1\\..\\x",
    ):
        stamina.redirect_to = sneaky
        with pytest.raises(StaminaApiError):
            await client.async_open_raw("/v1/cameras/38/redirect.m3u8")
    stamina.redirect_to = "redirect.m3u8"  # same origin, but a loop
    with pytest.raises(StaminaApiError, match="too many redirects"):
        await client.async_open_raw("/v1/cameras/38/redirect.m3u8")
    response, final = await client.async_open_raw("/v1/cameras/38/playback.m3u8", {"t": "1"})
    response.release()
    assert final.startswith("/v1/cameras/38/playback/pb_")


# ------------------------------------------------------------------ 15, 18, 19. the views


async def test_proxy_types_are_allowlisted_and_headers_harden(
    hass, hass_client_no_auth, stamina, setup
) -> None:
    from custom_components.scrypted_stamina.playback import proxy_url

    entry = await setup()
    client = await hass_client_no_auth()
    url = proxy_url(hass, entry.entry_id, f"/v1/cameras/38/playback/pb_{'a' * 22}/x.html")
    response = await client.get(url)
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("application/octet-stream")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "sandbox"

    playlist = await client.get(
        proxy_url(hass, entry.entry_id, "/v1/cameras/38/playback.m3u8?t=1&d=30")
    )
    text = await playlist.text()
    assert "192.0.2.9" not in text and "evil" not in text  # dropped, not passed through
    assert playlist.headers["Content-Security-Policy"] == "sandbox"
    await hass.config_entries.async_unload(entry.entry_id)


def test_unrewritable_uris_are_dropped() -> None:
    text = '#EXTM3U\n#EXT-X-MAP:URI="http://x/init.mp4"\n#EXTINF:2,\nok.m4s\n#EXTINF:2,\nbad\n'
    out = rewrite_playlist(text, lambda uri: f"/signed/{uri}" if uri == "ok.m4s" else None)
    assert "EXT-X-MAP" not in out and "bad" not in out and "/signed/ok.m4s" in out


def test_video_ranges_and_types() -> None:
    assert _byte_range(None, 10) is None
    assert _byte_range("bytes=2-5", 10) == (2, 5)
    assert _byte_range("bytes=8-", 10) == (8, 9)
    assert _byte_range("bytes=4-99", 10) == (4, 9)
    assert _byte_range("bytes=-3", 10) == (7, 9)
    assert _byte_range("bytes=10-12", 10) == (10, 9)  # past the end: 416
    for unusable in ("bytes=0-1,4-5", "items=0-1", "bytes=a-b", "bytes=5-2"):
        assert _byte_range(unusable, 10) is None  # served whole
    assert safe_type("text/html") == "application/octet-stream"
    assert safe_type("video/iso.segment") == "video/iso.segment"
    assert safe_type("application/x-mpegURL") == "application/vnd.apple.mpegurl"


# ------------------------------------------------------------------ 20, 31. limits, full reads


async def test_bodies_are_read_whole_and_within_their_limit(socket_enabled, aiohttp_server) -> None:
    """`StreamReader.read(n)` returns what has arrived; a slow body must not come back short."""

    async def slow(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        for _ in range(5):
            await response.write(b"x" * 1000)
            await asyncio.sleep(0.02)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/slow", slow)
    server = await aiohttp_server(app)
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(server.make_url("/slow")) as response:
            assert len(await async_read_limited(response, 10_000, "slow")) == 5000
        async with session.get(server.make_url("/slow")) as response:
            with pytest.raises(StaminaApiError, match="larger"):
                await async_read_limited(response, 2500, "slow")


def test_per_segment_metadata_is_bounded() -> None:
    facts = EventFacts(id="ev_1", camera="38", started=1.0)
    for n in range(5000):
        facts.segment_seen(n, init=False, duration=2.0, discontinuity=True)
    assert facts.segments == 5000
    assert len(facts.durations) <= 4096 and len(facts.discontinuities) <= 4096


# ------------------------------------------------------------------ 21. scrubbing


def test_destination_errors_are_scrubbed() -> None:
    target = Target(
        subentry_id="x",
        destination=FakeDestination(),
        folder="f",
        quota=1,
        retention_days=0,
        follow_up_cap=1,
    )
    assert target.failed(
        Exception("PUT https://192.0.2.4/x?token=abcdef failed: Bearer s3cr3t"), "closed", 0.0
    )
    assert "abcdef" not in target.last_error and "s3cr3t" not in target.last_error
    assert target.degraded
