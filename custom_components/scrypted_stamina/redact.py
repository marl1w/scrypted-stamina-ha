"""Keep credentials out of text that is about to be shown, logged or reported.

Ported from Reolink Stamina: literal secrets first (exact, so they catch a token quoted
without its parameter name), then patterns for whatever the caller could not name. Added
here: `Bearer` headers, and the `sig` of a signed playback URL.
"""

from __future__ import annotations

from collections.abc import Iterable
import re

# `password`, `token` and `sig` run to whitespace or a quote rather than stopping at `&`:
# stopping at `&` would leave the tail of a secret that contains one in plain view.
_SECRET = re.compile(r"\b(password|token|sig|access_token)=[^\s'\"]+")
_USER = re.compile(r"\buser=[^&\s'\"]+")
_BEARER = re.compile(r"\b(Bearer)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)

REDACTED = "***"


def scrub_credentials(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Blank credentials out of text bound for a log, a report or an attribute."""
    for secret in secrets:
        # A very short secret would otherwise redact half the message; the patterns below
        # still cover it by name.
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    text = _SECRET.sub(rf"\1={REDACTED}", text)
    text = _BEARER.sub(rf"\1 {REDACTED}", text)
    return _USER.sub(f"user={REDACTED}", text)
