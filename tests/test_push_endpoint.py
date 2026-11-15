"""The server only ever POSTs to a known push service: an endpoint is a URL a
signed-in browser hands us, and anything else would let it aim our server at
an arbitrary address."""
import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from core.config import settings
from core.services import push_endpoint


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/fcm/send/abc:def",
    "https://web.push.apple.com/QGx2",
    "https://wns2-bl2p.notify.windows.com/w/?token=x",
    "https://updates.push.services.mozilla.com/wpush/v2/gAAA",
    "https://fcm.googleapis.com:443/fcm/send/x",
    "https://FCM.googleapis.com/fcm/send/x",
])
def test_known_push_services_are_accepted(endpoint):
    assert push_endpoint.valid(endpoint)


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/x",
    "https://evil.com/fcm.googleapis.com",
    "https://fcm.googleapis.com.evil.com/x",
    "https://evilpush.apple.com/x",
    "https://push.apple.com.evil.com/x",
    "https://u:p@fcm.googleapis.com/x",
    "https://fcm.googleapis.com:8443/x",
    "https://127.0.0.1/x",
    "https://fcm.googleapis.com./x",
    "fcm.googleapis.com/x",
    "",
    "https://fcm.googleapis.com/" + "x" * 1024,
])
def test_anything_else_is_refused(endpoint):
    assert not push_endpoint.valid(endpoint)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _device_keys() -> tuple[str, str]:
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(public), _b64(b"0123456789abcdef")


def test_real_device_keys_are_accepted():
    assert push_endpoint.valid_keys(*_device_keys())


@pytest.mark.parametrize("which", ["short_key", "short_auth", "not_base64", "compressed"])
def test_malformed_device_keys_are_refused(which):
    p256dh, auth = _device_keys()
    if which == "short_key":
        p256dh = p256dh[:40]
    elif which == "short_auth":
        auth = auth[:10]
    elif which == "not_base64":
        p256dh = "!" * 87
    else:
        p256dh = _b64(b"\x02" + b"\x01" * 64)
    assert not push_endpoint.valid_keys(p256dh, auth)


def test_configured_only_with_all_three_keys(monkeypatch):
    monkeypatch.setattr(settings, "push_vapid_public_key", "pub")
    monkeypatch.setattr(settings, "push_vapid_private_key", "priv")
    monkeypatch.setattr(settings, "push_vapid_subject", "mailto:team@getconverge.me")
    assert push_endpoint.push_configured()
    monkeypatch.setattr(settings, "push_vapid_private_key", "")
    assert not push_endpoint.push_configured()
