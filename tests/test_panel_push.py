"""The panel's push routes: only a signed-in session from the panel's own
origin subscribes, only to a known push service, and always as itself."""
# ruff: noqa: F811
import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from core.config import settings
from core.repositories import panel_auth as auth_repo
from core.repositories import push as push_repo
from tests.test_panel import accounts, client, clock, signed_in  # noqa: F401

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


KEYS = {
    "p256dh": _b64(ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)),
    "auth": _b64(b"0123456789abcdef"),
}


@pytest.fixture
def stored(monkeypatch):
    """core.repositories.push, in memory: endpoint -> (session_id, user_id)."""
    rows: dict[str, tuple] = {}

    async def save(conn, session_id, user_id, endpoint, p256dh, auth):
        rows[endpoint] = (session_id, user_id)

    async def delete_own(conn, session_id, endpoint):
        if rows.get(endpoint, (None,))[0] == session_id:
            del rows[endpoint]
            return True
        return False

    async def session_ids_with_push(conn):
        return {session_id for session_id, _ in rows.values()}

    monkeypatch.setattr(push_repo, "save", save)
    monkeypatch.setattr(push_repo, "delete_own", delete_own)
    monkeypatch.setattr(push_repo, "session_ids_with_push", session_ids_with_push)
    return rows


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(settings, "push_vapid_public_key", "BPublicKey")
    monkeypatch.setattr(settings, "push_vapid_private_key", "private")
    monkeypatch.setattr(settings, "push_vapid_subject", "mailto:team@getconverge.me")


def _subscribe(client, **body):
    return client.post("/painel/api/push/subscribe",
                       json={"endpoint": ENDPOINT, "keys": KEYS, **body})


def test_without_keys_the_routes_do_not_exist(signed_in, stored):
    assert signed_in.get("/painel/api/push/key").status_code == 404
    assert _subscribe(signed_in).status_code == 404
    assert signed_in.request("DELETE", "/painel/api/push/subscribe",
                             json={"endpoint": ENDPOINT}).status_code == 404
    assert stored == {}


def test_without_a_session_nothing_is_stored(client, keys, stored):
    assert client.get("/painel/api/push/key").status_code == 401
    assert _subscribe(client).status_code == 401
    assert stored == {}


def test_the_public_key_is_given_to_the_app(signed_in, keys):
    response = signed_in.get("/painel/api/push/key")
    assert response.status_code == 200 and response.json() == {"public_key": "BPublicKey"}
    assert response.headers["cache-control"] == "no-store"


def test_another_site_cannot_subscribe(signed_in, keys, stored):
    response = signed_in.post("/painel/api/push/subscribe", json={"endpoint": ENDPOINT, "keys": KEYS},
                              headers={"Origin": "https://evil.example"})
    assert response.status_code == 403 and stored == {}


@pytest.mark.parametrize("body", [
    {"endpoint": "https://evil.example/push"},
    {"endpoint": "http://fcm.googleapis.com/fcm/send/abc"},
    {"keys": {"p256dh": "short", "auth": KEYS["auth"]}},
    {"keys": {"p256dh": KEYS["p256dh"]}},
])
def test_a_bad_subscription_is_refused(signed_in, keys, stored, body):
    assert _subscribe(signed_in, **body).status_code == 422
    assert stored == {}


def test_the_subscription_belongs_to_the_signed_in_session(signed_in, keys, stored, accounts):
    response = _subscribe(signed_in, session_id=999, user_id=999)
    assert response.status_code == 200 and response.json() == {"ok": True}
    ((session_id, user_id),) = stored.values()
    session = next(s for s in accounts.sessions.values() if s.get("id") == session_id)
    assert user_id == session["user_id"] != 999 and session_id != 999


def test_a_device_removes_only_its_own_subscription(signed_in, keys, stored):
    stored[ENDPOINT] = (12345, 1)  # another session's
    response = signed_in.request("DELETE", "/painel/api/push/subscribe", json={"endpoint": ENDPOINT})
    assert response.status_code == 200 and response.json() == {"ok": False}
    assert ENDPOINT in stored
    del stored[ENDPOINT]
    _subscribe(signed_in)
    response = signed_in.request("DELETE", "/painel/api/push/subscribe", json={"endpoint": ENDPOINT})
    assert response.json() == {"ok": True} and stored == {}


def test_devices_say_which_have_notifications(signed_in, keys, stored, monkeypatch):
    async def devices(conn, current_id, idle_days):
        return [{"id": 7, "name": "Ana"}, {"id": 8, "name": "Ana"}]

    monkeypatch.setattr(auth_repo, "devices", devices)
    stored["https://fcm.googleapis.com/fcm/send/x"] = (7, 1)
    rows = signed_in.get("/painel/api/devices").json()["devices"]
    assert [(row["id"], row["push"]) for row in rows] == [(7, True), (8, False)]
