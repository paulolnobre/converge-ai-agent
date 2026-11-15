"""/painel: nobody in without a password and an authenticator code, and only
what a card may change changes.

The HTTP layer runs for real — invite, sign-in, code, session, sign-out — on a
bare app, so the webhook's lifespan (Postgres, Redis, workers) never starts.
Underneath, the accounts live in memory with the same rules the SQL enforces:
an invite works once, a code is never accepted twice, five failures lock.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import content
from core.config import settings
from core.repositories import funnel as funnel_repo
from core.repositories import media as media_repo
from core.repositories import panel_auth as auth_repo
from core.repositories import push as push_repo
from core.services import panel_auth as auth
from core.web import panel

PASSWORD = "uma-senha-bem-longa-9"
EMAIL = "ana@converge.test"
LEAD = {
    "id": 1, "name": "Ana", "chat_id": "42", "email": None, "phone": None,
    "organization": None, "notes": None, "stage": "respondido",
    "stage_changed_at": None, "source": "telegram", "interest": None,
    "owner_staff_id": None, "owner_name": None, "created_at": None,
    "last_message_at": None, "last_message": "oi", "next_appointment_at": None,
}


class _Conn:
    async def commit(self):
        return None


class _Pool:
    @asynccontextmanager
    async def connection(self):
        yield _Conn()


async def _value(value):
    return value


class Accounts:
    """core.repositories.panel_auth, in memory."""

    def __init__(self):
        self.users: dict[int, dict] = {}
        self.invites: dict[str, dict] = {}
        self.recovery: dict[tuple[int, str], bool] = {}  # -> used
        self.sessions: dict[str, dict] = {}
        self.events: list[str] = []
        self.next_session_id = 1

    def add_user(self, email: str, name: str) -> int:
        user_id = len(self.users) + 1
        self.users[user_id] = {
            "id": user_id, "email": email, "name": name, "password_hash": None,
            "totp_secret_enc": None, "enrolled_at": None, "active": True,
            "failed_attempts": 0, "lock": False, "last_totp_step": None,
        }
        return user_id

    def _view(self, user):
        return None if user is None else {**user, "locked": user["lock"]}

    async def user_by_email(self, conn, email):
        return self._view(next((u for u in self.users.values() if u["email"] == email.lower()), None))

    async def user_by_id(self, conn, user_id):
        return self._view(self.users.get(user_id))

    async def invited_user(self, conn, token_hash):
        invite = self.invites.get(token_hash)
        if not invite or invite["used"] or not self.users[invite["user_id"]]["active"]:
            return None
        return self._view(self.users[invite["user_id"]])

    async def set_pending_totp(self, conn, user_id, secret_enc):
        if self.users[user_id]["enrolled_at"] is None:
            self.users[user_id]["totp_secret_enc"] = secret_enc

    async def complete_enrollment(self, conn, token_hash, user_id, password_hash, step, hashes):
        invite = self.invites.get(token_hash)
        if not invite or invite["used"]:
            return False
        invite["used"] = True
        self.users[user_id].update(password_hash=password_hash, enrolled_at=datetime.now(),
                                   last_totp_step=step, failed_attempts=0, lock=False)
        for code_hash in hashes:
            self.recovery[(user_id, code_hash)] = False
        return True

    async def set_password_hash(self, conn, user_id, password_hash):
        self.users[user_id]["password_hash"] = password_hash

    async def record_failure(self, conn, user_id, max_attempts, lock_minutes):
        user = self.users[user_id]
        user["failed_attempts"] += 1
        if user["failed_attempts"] >= max_attempts:
            user["lock"] = True
        return user["lock"]

    async def clear_failures(self, conn, user_id):
        self.users[user_id].update(failed_attempts=0, lock=False)

    async def accept_totp_step(self, conn, user_id, step):
        last = self.users[user_id]["last_totp_step"]
        if last is not None and step <= last:
            return False
        self.users[user_id]["last_totp_step"] = step
        return True

    async def use_recovery_code(self, conn, user_id, code_hash):
        if self.recovery.get((user_id, code_hash)) is False:
            self.recovery[(user_id, code_hash)] = True
            return True
        return False

    async def recovery_codes_left(self, conn, user_id):
        return sum(1 for (uid, _), used in self.recovery.items() if uid == user_id and not used)

    async def create_session(self, conn, token_hash, user_id, *, mfa, minutes, ip, user_agent,
                             kind="browser", device_name=None):
        self.sessions[token_hash] = {"user_id": user_id, "mfa": mfa, "kind": kind,
                                     "device_name": device_name, "id": self.next_session_id,
                                     "renew": False}
        self.next_session_id += 1

    async def session(self, conn, token_hash, *, mfa, idle_minutes, device_idle_days=90):
        found = self.sessions.get(token_hash)
        if not found or found["mfa"] != mfa:
            return None
        user = self.users[found["user_id"]]
        if not user["active"]:
            return None
        return {"user_id": user["id"], "email": user["email"], "name": user["name"], "stale": False,
                "kind": found.get("kind", "browser"), "session_id": found.get("id")}

    async def renew_device_cookie(self, conn, token_hash):
        found = self.sessions[token_hash]
        renew = found["renew"]
        found["renew"] = False
        return renew

    async def devices(self, conn, current_id, idle_days):
        return [{"id": s["id"], "name": self.users[s["user_id"]]["name"],
                 "device_name": s["device_name"], "is_current": s["id"] == current_id,
                 "created_at": datetime.now(), "last_seen_at": datetime.now()}
                for s in self.sessions.values() if s["mfa"] and s["kind"] == "device"]

    async def revoke_device(self, conn, session_id):
        token = next((k for k, s in self.sessions.items()
                      if s["id"] == session_id and s["mfa"] and s["kind"] == "device"), None)
        if token is None:
            return False
        del self.sessions[token]
        return True

    async def touch_session(self, conn, token_hash):
        return None

    async def delete_session(self, conn, token_hash):
        self.sessions.pop(token_hash, None)

    async def log_event(self, conn, event, *, user_id, email, ip):
        self.events.append(event)


class Clock:
    def __init__(self):
        self.now = 1_800_000_000

    def code(self, secret: str, steps_ahead: int = 0) -> str:
        return pyotp.TOTP(secret).at(self.now + 30 * steps_ahead)


@pytest.fixture
def accounts(monkeypatch: pytest.MonkeyPatch) -> Accounts:
    store = Accounts()
    for name in (
        "user_by_email", "user_by_id", "invited_user", "set_pending_totp",
        "complete_enrollment", "set_password_hash", "record_failure", "clear_failures",
        "accept_totp_step", "use_recovery_code", "recovery_codes_left", "create_session",
        "session", "touch_session", "delete_session", "log_event", "renew_device_cookie",
        "devices", "revoke_device",
    ):
        monkeypatch.setattr(auth_repo, name, getattr(store, name))
    return store


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fixed = Clock()
    monkeypatch.setattr(auth.time, "time", lambda: fixed.now)
    return fixed


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, accounts, clock) -> TestClient:
    monkeypatch.setattr(settings, "panel_secret_key", Fernet.generate_key().decode())
    panel._failures._events.clear()
    panel._failures._notified.clear()

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(panel, "get_pool", get_pool)
    # The Inbox reads the media of a conversation's lines; none here.
    monkeypatch.setattr(media_repo, "for_sessions", lambda conn, session_ids: _value({}))
    monkeypatch.setattr(funnel_repo, "list_leads", lambda conn: _value([LEAD]))
    monkeypatch.setattr(funnel_repo, "staff", lambda conn: _value([]))
    # No push subscription unless a test stores one (tests/test_panel_push.py).
    monkeypatch.setattr(push_repo, "session_ids_with_push", lambda conn: _value(set()))
    monkeypatch.setattr(
        funnel_repo, "lead",
        lambda conn, contact_id: _value({**LEAD, "messages": [], "appointments": []}),
    )
    app = FastAPI()
    panel.install(app)
    # https: the cookies are Secure, and a client on http would never send them back.
    return TestClient(app, base_url="https://testserver")


def enroll(client: TestClient, accounts: Accounts, clock: Clock,
           email: str = EMAIL) -> tuple[str, list[str]]:
    """Accept an invite the way a person does. Leaves the client signed in."""
    user_id = accounts.add_user(email, "Ana")
    token = auth.new_token()
    accounts.invites[auth.digest(token)] = {"user_id": user_id, "used": False}
    start = client.post("/painel/api/auth/invite/start", json={"token": token})
    assert start.status_code == 200, start.text
    secret = start.json()["secret"]
    finish = client.post("/painel/api/auth/invite/finish", json={
        "token": token, "password": PASSWORD, "code": clock.code(secret),
    })
    assert finish.status_code == 200, finish.text
    return secret, finish.json()["recovery_codes"]


def sign_in(client: TestClient, clock: Clock, secret: str, *, steps_ahead: int = 1,
            email: str = EMAIL, password: str = PASSWORD):
    assert client.post("/painel/api/auth/login",
                       json={"email": email, "password": password}).status_code == 200
    return client.post("/painel/api/auth/code",
                       json={"code": clock.code(secret, steps_ahead)})


@pytest.fixture
def signed_in(client, accounts, clock) -> TestClient:
    enroll(client, accounts, clock)
    return client


# ── Closed by default ────────────────────────────────────────────────────────

def test_without_the_encryption_key_the_panel_is_closed(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "panel_secret_key", "")
    assert client.get("/painel/api/leads").status_code == 503
    assert client.post("/painel/api/auth/login",
                       json={"email": EMAIL, "password": PASSWORD}).status_code == 503


def test_without_a_session_the_data_is_refused_and_the_page_sends_to_sign_in(client):
    assert client.get("/painel/api/leads").status_code == 401
    page = client.get("/painel", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/painel/entrar"
    assert client.get("/painel/entrar").status_code == 200
    assert client.get("/painel/convite").status_code == 200


def test_assets_are_a_fixed_list(client):
    for name in ("painel.js", "painel.css", "auth.js", "auth.css", "automacoes.js", "inbox.js"):
        assert client.get(f"/painel/assets/{name}").status_code == 200
    for name in ("index.html", "..%2F..%2Fpanel.py", "favicon.svg", "entrar.html"):
        assert client.get(f"/painel/assets/{name}").status_code == 404


def test_every_panel_response_is_hardened(client):
    headers = client.get("/painel/entrar").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["strict-transport-security"].startswith("max-age=")


# ── Enrollment ───────────────────────────────────────────────────────────────

def test_an_invite_sets_password_and_authenticator_and_signs_in(client, accounts, clock):
    secret, codes = enroll(client, accounts, clock)

    user = accounts.users[1]
    assert user["password_hash"].startswith("$argon2id$")
    assert user["totp_secret_enc"] != secret, "the TOTP secret is never stored in the clear"
    assert auth.decrypt(user["totp_secret_enc"]) == secret
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(code not in str(accounts.recovery) for code in codes), "only hashes are kept"
    assert client.get("/painel/api/leads").status_code == 200


def test_the_session_cookie_cannot_be_read_by_scripts_and_survives_a_link_from_telegram(
        client, accounts, clock):
    """Lax, not Strict: "Abrir no painel" tapped in Telegram is a navigation
    from another app, and a Strict cookie is left off it — the panel then asked
    for a sign-in with a valid session. Lax still keeps the cookie off every
    cross-site request that is not a top-level navigation."""
    user_id = accounts.add_user(EMAIL, "Ana")
    token = auth.new_token()
    accounts.invites[auth.digest(token)] = {"user_id": user_id, "used": False}
    secret = client.post("/painel/api/auth/invite/start", json={"token": token}).json()["secret"]
    finish = client.post("/painel/api/auth/invite/finish",
                         json={"token": token, "password": PASSWORD, "code": clock.code(secret)})
    cookie = finish.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=lax", "path=/painel"):
        assert flag in cookie
    assert "samesite=strict" not in cookie
    assert list(accounts.sessions) != [client.cookies.get(panel.SESSION_COOKIE)], (
        "the database keeps the hash, not the token"
    )


def test_an_invite_works_once(client, accounts, clock):
    user_id = accounts.add_user(EMAIL, "Ana")
    token = auth.new_token()
    accounts.invites[auth.digest(token)] = {"user_id": user_id, "used": False}
    secret = client.post("/painel/api/auth/invite/start", json={"token": token}).json()["secret"]
    body = {"token": token, "password": PASSWORD, "code": clock.code(secret)}
    assert client.post("/painel/api/auth/invite/finish", json=body).status_code == 200
    assert client.post("/painel/api/auth/invite/start", json={"token": token}).status_code == 404


def test_a_bad_invite_a_weak_password_and_a_wrong_code_are_refused(client, accounts, clock):
    assert client.post("/painel/api/auth/invite/start",
                       json={"token": "nope"}).status_code == 404
    user_id = accounts.add_user(EMAIL, "Ana")
    token = auth.new_token()
    accounts.invites[auth.digest(token)] = {"user_id": user_id, "used": False}
    secret = client.post("/painel/api/auth/invite/start", json={"token": token}).json()["secret"]

    weak = client.post("/painel/api/auth/invite/finish",
                       json={"token": token, "password": "curta", "code": clock.code(secret)})
    assert weak.status_code == 400 and "12" in weak.json()["detail"]
    wrong = client.post("/painel/api/auth/invite/finish",
                        json={"token": token, "password": PASSWORD, "code": "000000"})
    assert wrong.status_code == 400
    assert accounts.users[user_id]["enrolled_at"] is None


# ── Signing in ───────────────────────────────────────────────────────────────

def test_password_then_code_signs_in(client, accounts, clock):
    secret, _ = enroll(client, accounts, clock)
    client.cookies.clear()

    response = sign_in(client, clock, secret)

    assert response.status_code == 200
    assert client.get("/painel/api/me").json()["name"] == "Ana"
    assert client.get("/painel").status_code == 200


def test_the_password_alone_opens_nothing(client, accounts, clock):
    enroll(client, accounts, clock)
    client.cookies.clear()
    client.post("/painel/api/auth/login", json={"email": EMAIL, "password": PASSWORD})

    assert client.get("/painel/api/leads").status_code == 401
    # Renaming a cookie is trivial in any browser: what keeps the half-way
    # token out is the session being marked as still owing the code.
    half_way = client.cookies.get(panel.PRE_COOKIE)
    assert half_way
    client.cookies.clear()
    client.cookies.set(panel.SESSION_COOKIE, half_way, path="/painel")
    assert client.get("/painel/api/leads").status_code == 401


def test_wrong_password_and_unknown_email_look_the_same(client, accounts, clock):
    enroll(client, accounts, clock)
    client.cookies.clear()
    wrong = client.post("/painel/api/auth/login", json={"email": EMAIL, "password": "errada-errada"})
    nobody = client.post("/painel/api/auth/login",
                         json={"email": "ninguem@x.com", "password": PASSWORD})
    assert wrong.status_code == nobody.status_code == 401
    assert wrong.json() == nobody.json()


def test_a_code_is_never_accepted_twice(client, accounts, clock):
    secret, _ = enroll(client, accounts, clock)
    client.cookies.clear()
    assert sign_in(client, clock, secret, steps_ahead=1).status_code == 200

    client.cookies.clear()
    replay = sign_in(client, clock, secret, steps_ahead=1)

    assert replay.status_code == 401


def test_a_code_without_the_password_step_is_refused(client, accounts, clock):
    secret, _ = enroll(client, accounts, clock)
    client.cookies.clear()
    response = client.post("/painel/api/auth/code", json={"code": clock.code(secret, 1)})
    assert response.status_code == 401


def test_a_recovery_code_works_once(client, accounts, clock):
    secret, codes = enroll(client, accounts, clock)
    client.cookies.clear()

    client.post("/painel/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    first = client.post("/painel/api/auth/code", json={"code": codes[0].upper().replace("-", " ")})
    assert first.status_code == 200 and first.json()["recovery_left"] == 9

    client.cookies.clear()
    client.post("/painel/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    again = client.post("/painel/api/auth/code", json={"code": codes[0]})
    assert again.status_code == 401


def test_five_failures_lock_the_account_even_for_the_right_password(client, accounts, clock):
    enroll(client, accounts, clock)
    client.cookies.clear()
    statuses = [
        client.post("/painel/api/auth/login",
                    json={"email": EMAIL, "password": f"errada-{i}-xxxxx"}).status_code
        for i in range(5)
    ]
    assert statuses[:4] == [401] * 4 and statuses[4] == 429
    right = client.post("/painel/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert right.status_code == 429
    assert "locked" in accounts.events


def test_one_address_guessing_many_emails_is_stopped(client, accounts, clock):
    statuses = [
        client.post("/painel/api/auth/login",
                    json={"email": f"p{i}@x.com", "password": "qualquer-coisa"}).status_code
        for i in range(12)
    ]
    assert statuses[:10] == [401] * 10 and statuses[-1] == 429


def test_signing_out_ends_the_session(signed_in):
    assert signed_in.get("/painel/api/leads").status_code == 200
    assert signed_in.post("/painel/api/auth/logout").status_code == 200
    assert signed_in.get("/painel/api/leads").status_code == 401


def test_a_disabled_account_is_signed_out_at_once(signed_in, accounts):
    accounts.users[1]["active"] = False
    assert signed_in.get("/painel/api/leads").status_code == 401


def test_another_site_cannot_make_changes(signed_in):
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        response = signed_in.patch("/painel/api/leads/1", json={"stage": "proposta"},
                                   headers=headers)
        assert response.status_code == 403
        login = signed_in.post("/painel/api/auth/login",
                               json={"email": EMAIL, "password": PASSWORD}, headers=headers)
        assert login.status_code == 403


# ── The data, once in ────────────────────────────────────────────────────────

def test_the_data_is_never_cached(signed_in):
    api = signed_in.get("/painel/api/leads")
    assert api.status_code == 200
    assert api.json()["leads"][0]["name"] == "Ana"
    assert api.headers["cache-control"] == "no-store"


def test_only_editable_fields_that_were_sent_are_written(signed_in, monkeypatch: pytest.MonkeyPatch):
    written = {}

    async def update_lead(conn, contact_id, fields):
        written.update(fields)
        return True

    monkeypatch.setattr(funnel_repo, "update_lead", update_lead)
    response = signed_in.patch(
        "/painel/api/leads/1",
        json={"stage": "proposta", "owner_staff_id": None, "chat_id": "hijack"},
    )
    assert response.status_code == 200
    # chat_id is not a panel field; owner_staff_id=null was sent, so it unassigns.
    assert written == {"stage": "proposta", "owner_staff_id": None}


def test_an_unknown_stage_is_rejected(signed_in):
    assert signed_in.patch("/painel/api/leads/1", json={"stage": "ganho"}).status_code == 422


def test_the_stage_cannot_be_emptied(signed_in):
    assert signed_in.patch("/painel/api/leads/1", json={"stage": None}).status_code == 400


def test_the_page_escapes_what_strangers_write():
    """Names and messages come from anyone on Telegram; the script builds HTML
    from them, so every interpolation of contact data must go through esc()."""
    script = (panel._STATIC / "painel.js").read_text(encoding="utf-8")
    assert "function esc(" in script
    assert "'<div class=\"lcard-top\"><span class=\"lcard-name\">' + esc(l.name)" in script


def test_no_panel_page_runs_inline_script():
    """The CSP allows only same-origin script files; an inline one would be
    blocked in the browser, so it must not exist in the first place."""
    for name in ("index.html", "entrar.html", "convite.html"):
        html = (panel._STATIC / name).read_text(encoding="utf-8")
        assert "<script>" not in html and " onclick=" not in html


def test_adding_a_lead_lives_in_the_leads_tab():
    html = (panel._STATIC / "index.html").read_text(encoding="utf-8")
    leads = html.split('id="view-leads"', 1)[1].split("</section>", 1)[0]
    inbox = html.split('id="view-inbox"', 1)[1].split("</section>", 1)[0]
    assert 'id="lead-open"' in leads and 'id="lead-notice"' in leads
    assert 'id="lead-open"' not in inbox


def test_the_inbox_writes_what_strangers_send_as_text_only():
    script = (panel._STATIC / "inbox.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "textContent" in script


def test_signing_in_returns_only_to_an_inbox_conversation():
    """The button in Telegram opens /painel#inbox/<id>; after signing in the
    person lands there — and on nothing else a link could put after the #."""
    for name in ("auth.js", "painel.js", "inbox.js"):
        script = (panel._STATIC / name).read_text(encoding="utf-8")
        assert "/^#inbox\\/\\d+$/" in script, name


def test_the_tab_icon_is_public(client):
    response = client.get("/painel/favicon.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


# ── Automations ──────────────────────────────────────────────────────────────

AUTOMATION = {"id": 1, "kind": "reactivate", "enabled": False, "mode": "approve", "days": 60,
              "message": "Olá, {nome}!"}
TEAM_ONLY = {"id": 4, "kind": "no_proposal", "enabled": False, "mode": "approve", "days": 3,
             "message": ""}


@pytest.fixture
def automations_store(monkeypatch: pytest.MonkeyPatch):
    from core.repositories import automations as automations_repo

    store = {"updates": [], "decisions": []}
    by_id = {1: AUTOMATION, 4: TEAM_ONLY}
    monkeypatch.setattr(automations_repo, "list_automations",
                        lambda conn: _value([{**AUTOMATION, "pending": 0, "sent_30d": 0, "replied_30d": 0}]))
    monkeypatch.setattr(automations_repo, "pending", lambda conn: _value([]))
    monkeypatch.setattr(automations_repo, "reasons", lambda conn: _value({}))
    monkeypatch.setattr(automations_repo, "get_automation",
                        lambda conn, automation_id: _value(by_id.get(automation_id)))

    async def update_automation(conn, automation_id, fields, by):
        store["updates"].append((automation_id, fields, by))
        return True

    async def decide(conn, ids, approve, by):
        store["decisions"].append((ids, approve, by))
        return len(ids)

    monkeypatch.setattr(automations_repo, "update_automation", update_automation)
    monkeypatch.setattr(automations_repo, "decide", decide)
    return store


def test_automations_need_a_session(client, automations_store):
    assert client.get("/painel/api/automations").status_code == 401
    assert client.patch("/painel/api/automations/1", json={"enabled": True}).status_code == 401


def test_the_automations_tab_lists_every_whatsapp_template(
    signed_in, automations_store, monkeypatch: pytest.MonkeyPatch
):
    from core.services import wa_templates

    for name, value in {"whatsapp_app_secret": "s", "whatsapp_phone_number_id": "PN1",
                        "whatsapp_access_token": "t", "whatsapp_webhook_verify_token": "v"}.items():
        monkeypatch.setattr(settings, name, value)
    for template in wa_templates.CATALOG:
        monkeypatch.setattr(settings, template.setting, None)
    monkeypatch.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")
    monkeypatch.setattr(settings, "whatsapp_charge_template", "cobranca_enviada")

    body = signed_in.get("/painel/api/automations").json()
    templates = body["templates"]
    assert body["whatsapp_ready"] is True

    assert [t["key"] for t in templates] == [t.key for t in wa_templates.CATALOG]
    by_key = {t["key"]: t for t in templates}
    assert by_key["team"] == {"key": "team", "label": "Resposta da equipe",
                              "name": "resposta_da_equipe",
                              "purpose": "Inbox: reabrir a conversa para a equipe responder",
                              "manual": True, "active": True}
    assert by_key["charge"]["active"] is True and by_key["charge"]["manual"] is False
    assert by_key["resume"]["active"] is False and by_key["resume"]["name"] is None


def test_without_whatsapp_the_tab_says_so_and_keeps_the_names(
    signed_in, automations_store, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "whatsapp_access_token", None)
    monkeypatch.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")

    body = signed_in.get("/painel/api/automations").json()

    assert body["whatsapp_ready"] is False
    team = next(t for t in body["templates"] if t["key"] == "team")
    assert team["name"] == "resposta_da_equipe" and team["active"] is False


def test_a_change_is_saved_with_who_made_it(signed_in, automations_store):
    response = signed_in.patch("/painel/api/automations/1",
                               json={"enabled": True, "mode": "auto", "days": 45,
                                     "message": "  Oi, {nome}!  "})
    assert response.status_code == 200
    assert automations_store["updates"] == [
        (1, {"enabled": True, "mode": "auto", "days": 45, "message": "Oi, {nome}!"}, EMAIL)
    ]


@pytest.mark.parametrize(("change", "status"), [
    ({"message": "Oi, {cliente}!"}, 400),
    ({"message": ""}, 400),
    ({"days": 0}, 422),
    ({"days": 400}, 422),
    ({"mode": "sempre"}, 422),
    ({"enabled": None}, 400),
])
def test_a_bad_change_is_refused(signed_in, automations_store, change, status):
    assert signed_in.patch("/painel/api/automations/1", json=change).status_code == status
    assert automations_store["updates"] == []


def test_the_team_only_automation_has_no_message_to_edit(signed_in, automations_store):
    signed_in.patch("/painel/api/automations/4", json={"message": "Oi!", "days": 5})
    assert automations_store["updates"] == [(4, {"days": 5}, EMAIL)]


def test_approving_records_who_approved(signed_in, automations_store):
    response = signed_in.post("/painel/api/automations/queue", json={"ids": [3, 4], "approve": True})
    assert response.status_code == 200 and response.json()["changed"] == 2
    assert automations_store["decisions"] == [([3, 4], True, EMAIL)]


# ── Inbox ────────────────────────────────────────────────────────────────────

CONVERSATION = {"contact_id": 7, "chat_id": "42", "name": "Ana", "phone": None, "email": None,
                "stage": "respondido", "channel": "telegram", "origin": None, "origin_detail": None,
                "session_id": None, "state": "bot", "owner_name": None, "owner_user_id": None,
                "mine": False}


@pytest.fixture
def inbox_store(monkeypatch: pytest.MonkeyPatch):
    from core.repositories import inbox as inbox_repo
    from core.services import inbox

    store: dict = {"lists": [], "after": [], "actions": [], "origins": [], "refuse": None}

    async def list_conversations(conn, *, user_id, state="all", channel=None, limit=200):
        store["lists"].append((user_id, state, channel))
        return [{"contact_id": 7, "name": "Ana", "channel": "telegram", "origin": None,
                 "state": "bot", "owner_name": None, "owner_user_id": None, "mine": False,
                 "last_role": "user", "last_message": "oi", "last_at": None}]

    async def conversation(conn, contact_id, user_id):
        return dict(CONVERSATION) if contact_id == 7 else None

    async def messages(conn, contact_id, after_id=None, limit=200):
        store["after"].append(after_id)
        return [{"id": 1, "role": "user", "message": "oi", "intent": None,
                 "created_at": None, "author": None}]

    async def set_origin(conn, contact_id, origin, detail, user_id):
        store["origins"].append((contact_id, origin, detail, user_id))
        return contact_id == 7

    def action(name):
        async def run(conn, contact_id, user, *args, **kwargs):
            if store["refuse"]:
                raise inbox.InboxError(*store["refuse"])
            store["actions"].append((name, contact_id, user["user_id"], *args, *kwargs.values()))
        return run

    monkeypatch.setattr(inbox_repo, "list_conversations", list_conversations)
    monkeypatch.setattr(inbox_repo, "count_waiting", lambda conn: _value(2))
    monkeypatch.setattr(inbox_repo, "conversation", conversation)
    monkeypatch.setattr(inbox_repo, "messages", messages)
    monkeypatch.setattr(inbox_repo, "set_origin", set_origin)
    for name in ("claim", "release", "reply", "reopen"):
        monkeypatch.setattr(inbox, name, action(name))
    return store


def test_the_inbox_needs_a_session(client, inbox_store):
    assert client.get("/painel/api/inbox").status_code == 401
    assert client.post("/painel/api/inbox/7/claim").status_code == 401
    assert client.post("/painel/api/inbox/7/reply", json={"text": "oi"}).status_code == 401
    assert client.patch("/painel/api/contacts/7/origin", json={"origin": "site"}).status_code == 401


def test_the_list_is_filtered_for_whoever_asks(signed_in, inbox_store):
    body = signed_in.get("/painel/api/inbox?filter=mine&channel=telegram").json()

    assert inbox_store["lists"] == [(1, "mine", "telegram")]
    assert body["waiting_total"] == 2 and body["conversations"][0]["name"] == "Ana"


@pytest.mark.parametrize("query", ["?filter=tudo", "?channel=sms"])
def test_an_unknown_filter_is_refused(signed_in, inbox_store, query):
    assert signed_in.get("/painel/api/inbox" + query).status_code == 422


def test_a_conversation_never_exposes_the_chat_id(signed_in, inbox_store):
    body = signed_in.get("/painel/api/inbox/7?after=40").json()

    assert "chat_id" not in body["contact"]
    assert inbox_store["after"] == [40] and body["messages"][0]["message"] == "oi"
    assert signed_in.get("/painel/api/inbox/99").status_code == 404


def test_the_conversation_says_when_the_whatsapp_window_is_closed(
    signed_in, inbox_store, monkeypatch: pytest.MonkeyPatch
):
    from core.repositories import inbox as inbox_repo

    async def on_whatsapp(conn, contact_id, user_id):
        return {**CONVERSATION, "channel": "whatsapp"}

    monkeypatch.setattr(inbox_repo, "conversation", on_whatsapp)
    monkeypatch.setattr(inbox_repo, "window_open", lambda conn, contact_id: _value(False))

    contact = signed_in.get("/painel/api/inbox/7").json()["contact"]

    assert contact["window_open"] is False
    assert contact["window_notice"] == content.text("inbox.window_closed")


def test_past_the_window_the_conversation_lists_the_templates_to_choose(
    signed_in, inbox_store, monkeypatch: pytest.MonkeyPatch
):
    from core.repositories import inbox as inbox_repo
    from core.services import wa_templates

    async def on_whatsapp(conn, contact_id, user_id):
        return {**CONVERSATION, "channel": "whatsapp"}

    monkeypatch.setattr(inbox_repo, "conversation", on_whatsapp)
    monkeypatch.setattr(inbox_repo, "window_open", lambda conn, contact_id: _value(False))
    for name, value in {"whatsapp_app_secret": "s", "whatsapp_phone_number_id": "PN1",
                        "whatsapp_access_token": "t", "whatsapp_webhook_verify_token": "v"}.items():
        monkeypatch.setattr(settings, name, value)
    for template in wa_templates.CATALOG:
        monkeypatch.setattr(settings, template.setting, None)
    monkeypatch.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")
    monkeypatch.setattr(settings, "whatsapp_resume_template", "retomada_de_conversa")
    monkeypatch.setattr(settings, "whatsapp_charge_template", "cobranca_enviada")

    contact = signed_in.get("/painel/api/inbox/7").json()["contact"]

    assert contact["can_reopen"] is True
    assert contact["reopen_choices"] == [
        {"key": "team", "label": "Resposta da equipe"},
        {"key": "resume", "label": "Retomar conversa parada"},
    ]
    assert contact["window_notice"] == content.text("inbox.reopen_notice")


def test_reopen_carries_the_chosen_template(signed_in, inbox_store):
    assert signed_in.post("/painel/api/inbox/7/reopen", json={"template": "resume"}).status_code == 200
    assert signed_in.post("/painel/api/inbox/7/reopen").status_code == 200
    assert signed_in.post("/painel/api/inbox/7/reopen",
                          json={"template": "team", "reply": "Segue o valor."}).status_code == 200

    assert inbox_store["actions"] == [("reopen", 7, 1, "resume", None), ("reopen", 7, 1, None, None),
                                      ("reopen", 7, 1, "team", "Segue o valor.")]


def test_a_reopen_reply_too_long_is_refused(signed_in, inbox_store):
    long = "x" * 4001  # inbox.MAX_REPLY + 1
    assert signed_in.post("/painel/api/inbox/7/reopen", json={"reply": long}).status_code == 422
    assert inbox_store["actions"] == []


def test_a_telegram_conversation_has_no_window(signed_in, inbox_store):
    contact = signed_in.get("/painel/api/inbox/7").json()["contact"]

    assert contact["window_open"] is True and contact["window_notice"] is None


def test_claiming_and_answering_go_as_whoever_is_signed_in(signed_in, inbox_store):
    claim = {"state": "claimed", "owner_user_id": 2}
    assert signed_in.post("/painel/api/inbox/7/claim", json=claim).status_code == 200
    assert signed_in.post("/painel/api/inbox/7/reply", json={"text": "Oi Ana"}).status_code == 200
    assert signed_in.post("/painel/api/inbox/7/release").status_code == 200

    assert inbox_store["actions"] == [
        ("claim", 7, 1, "claimed", 2), ("reply", 7, 1, "Oi Ana"), ("release", 7, 1),
    ]


@pytest.mark.parametrize("body", [None, {}, {"state": "claimed"}, {"state": "tudo", "owner_user_id": None}])
def test_claiming_must_say_what_the_screen_showed(signed_in, inbox_store, body):
    """Without the state the person saw, a stale "Assumir" would take a
    conversation from whoever got there first."""
    response = signed_in.post("/painel/api/inbox/7/claim", json=body)
    assert response.status_code == 422 and inbox_store["actions"] == []


@pytest.mark.parametrize(("status", "detail"), [
    (409, "Victor assumiu esta conversa."),
    (502, "O Telegram não entregou a mensagem."),
    (501, "Canal ainda não conectado."),
])
def test_a_refusal_reaches_the_screen_as_it_is(signed_in, inbox_store, status, detail):
    inbox_store["refuse"] = (status, detail)

    response = signed_in.post("/painel/api/inbox/7/reply", json={"text": "Oi"})

    assert response.status_code == status and response.json()["detail"] == detail


@pytest.mark.parametrize("text", ["", "x" * 4001])
def test_an_empty_or_too_long_reply_never_reaches_the_service(signed_in, inbox_store, text):
    assert signed_in.post("/painel/api/inbox/7/reply", json={"text": text}).status_code == 422
    assert inbox_store["actions"] == []


def test_another_site_cannot_answer_for_you(signed_in, inbox_store):
    response = signed_in.post("/painel/api/inbox/7/reply", json={"text": "oi"},
                              headers={"Origin": "https://evil.example"})
    assert response.status_code == 403 and inbox_store["actions"] == []


def test_the_origin_is_saved_with_who_set_it(signed_in, inbox_store):
    ok = signed_in.patch("/painel/api/contacts/7/origin",
                         json={"origin": "indicacao", "detail": "  João  "})
    cleared = signed_in.patch("/painel/api/contacts/7/origin", json={"origin": None, "detail": ""})

    assert ok.status_code == 200 and cleared.status_code == 200
    assert inbox_store["origins"] == [(7, "indicacao", "João", 1), (7, None, None, 1)]


@pytest.mark.parametrize("change", [{"origin": "tiktok"}, {"origin": "site", "detail": "x" * 161}])
def test_an_unknown_origin_is_refused(signed_in, inbox_store, change):
    assert signed_in.patch("/painel/api/contacts/7/origin", json=change).status_code == 422
    assert inbox_store["origins"] == []


def test_a_change_started_by_another_site_is_refused_even_signed_in(signed_in):
    """With a Lax cookie, the origin check is what keeps another site from
    changing anything: every non-GET call must come from the panel itself."""
    response = signed_in.patch("/painel/api/leads/1", json={"stage": "perdido"},
                               headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403
    response = signed_in.patch("/painel/api/leads/1", json={"stage": "perdido"},
                               headers={"origin": "https://outro-site.example"})
    assert response.status_code == 403
