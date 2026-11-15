"""What the panel accepts as a push subscription.

The endpoint is a URL the browser hands us and the server POSTs to, so it must
be one of the known push services: anything else would let a signed-in page
aim our server at an arbitrary address. The device keys must have the sizes
Web Push encryption needs (RFC 8291).
"""
from __future__ import annotations

import base64
import binascii
from urllib.parse import urlsplit

from core.config import settings

MAX_ENDPOINT = 1024
_HOSTS = frozenset({"fcm.googleapis.com", "updates.push.services.mozilla.com"})
_SUFFIXES = (".push.apple.com", ".notify.windows.com")


def push_configured() -> bool:
    return bool(settings.push_vapid_public_key and settings.push_vapid_private_key
                and settings.push_vapid_subject)


def valid(endpoint: str) -> bool:
    if not endpoint or len(endpoint) > MAX_ENDPOINT:
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return False
    return host in _HOSTS or any(host.endswith(suffix) and len(host) > len(suffix)
                                 for suffix in _SUFFIXES)


def _decoded(value: str) -> bytes | None:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None


def valid_keys(p256dh: str, auth: str) -> bool:
    if not isinstance(p256dh, str) or not isinstance(auth, str) or len(p256dh) > 200 or len(auth) > 100:
        return False
    point, secret = _decoded(p256dh), _decoded(auth)
    return (point is not None and len(point) == 65 and point[0] == 4
            and secret is not None and len(secret) == 16)
