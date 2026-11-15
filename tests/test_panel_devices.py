"""Trusted devices require both factors; revocation is immediate and same-origin."""
# ruff: noqa: F811
from core.config import settings
from core.services import panel_auth as auth
from core.web import panel
from tests.test_panel import (  # noqa: F401
    EMAIL,
    PASSWORD,
    accounts,
    client,
    clock,
    enroll,
    signed_in,
)


def _device_login(client, clock, secret):
    client.cookies.clear()
    assert client.post("/painel/api/auth/login", json={
        "email": EMAIL, "password": PASSWORD, "remember_device": True,
    }, headers={"User-Agent": "Mozilla/5.0 (iPhone) AppleWebKit Safari"}).status_code == 200
    return client.post("/painel/api/auth/code", json={"code": clock.code(secret, 1)})


def test_device_login_still_requires_mfa(client, accounts, clock):
    secret, _ = enroll(client, accounts, clock)
    client.cookies.clear()
    first = client.post("/painel/api/auth/login", json={
        "email": EMAIL, "password": PASSWORD, "remember_device": True,
    })
    assert first.status_code == 200
    assert client.get("/painel/api/devices").status_code == 401
    pre = client.cookies.get(panel.PRE_COOKIE)
    client.cookies.set(panel.SESSION_COOKIE, pre, path="/painel")
    assert client.get("/painel/api/me").status_code == 401
    assert client.post("/painel/api/auth/code", json={
        "code": clock.code(secret, 1),
    }).status_code == 200
    assert "device_signed_in" in accounts.events


def test_device_cookie_is_secure_and_the_preference_survives_the_password_step(client, accounts, clock):
    secret, _ = enroll(client, accounts, clock)
    response = _device_login(client, clock, secret)
    assert response.status_code == 200
    cookie = response.headers.get_list("set-cookie")[0].lower()
    for flag in ("httponly", "secure", "samesite=lax", "path=/painel",
                 f"max-age={settings.panel_device_idle_days * 86400}"):
        assert flag in cookie
    listing = client.get("/painel/api/devices")
    assert listing.headers["cache-control"] == "no-store"
    device, = listing.json()["devices"]
    assert device["is_current"] is True
    # Second-factor request's label, never any client-supplied arbitrary name.
    assert "token" not in listing.text and "hash" not in listing.text


def test_device_cookie_renews_once_and_not_on_every_poll(client, accounts, clock):
    secret, _ = enroll(client, accounts, clock)
    _device_login(client, clock, secret)
    token = client.cookies.get(panel.SESSION_COOKIE)
    assert token is not None
    accounts.sessions[auth.digest(token)]["renew"] = True
    assert "set-cookie" in client.get("/painel/api/me").headers
    assert "set-cookie" not in client.get("/painel/api/me").headers


def test_browser_login_remains_short_and_is_not_listed_as_a_trusted_device(signed_in, accounts):
    assert signed_in.get("/painel/api/devices").json() == {"devices": []}
    assert "device_signed_in" not in accounts.events


def test_revoke_current_device_clears_cookie_and_blocks_the_saved_token(client, accounts, clock):
    secret, _ = enroll(client, accounts, clock)
    _device_login(client, clock, secret)
    token = client.cookies.get(panel.SESSION_COOKIE)
    assert token is not None
    accounts.sessions[auth.digest(token)]["renew"] = True
    device, = client.get("/painel/api/devices").json()["devices"]
    accounts.sessions[auth.digest(token)]["renew"] = True
    response = client.delete(f"/painel/api/devices/{device['id']}")
    assert response.json() == {"ok": True, "current": True}
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert "device_revoked" in accounts.events
    client.cookies.set(panel.SESSION_COOKIE, token, path="/painel")
    assert client.get("/painel/api/me").status_code == 401


def test_another_team_member_can_revoke_a_device_but_cross_site_cannot(client, accounts, clock):
    secret, _ = enroll(client, accounts, clock)
    browser_token = client.cookies.get(panel.SESSION_COOKIE)
    _device_login(client, clock, secret)
    device, = client.get("/painel/api/devices").json()["devices"]
    # A second authenticated team account has the same permission.
    uid = accounts.add_user("bia@converge.test", "Bia")
    assert browser_token is not None
    accounts.sessions[auth.digest(browser_token)]["user_id"] = uid
    client.cookies.clear()
    client.cookies.set(panel.SESSION_COOKIE, browser_token, path="/painel")
    path = f"/painel/api/devices/{device['id']}"
    assert client.delete(path, headers={"Origin": "https://other.test"}).status_code == 403
    assert client.delete(path, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.delete(path).json() == {"ok": True, "current": False}
    assert client.get("/painel/api/me").status_code == 200
    assert client.delete(path).status_code == 404


def test_devices_require_a_full_session(client):
    assert client.get("/painel/api/devices").status_code == 401
    assert client.delete("/painel/api/devices/1").status_code == 401
