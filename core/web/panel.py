"""/painel: the funnel dashboard, the API behind it, and signing in to it.

Served by the same container as the webhook rather than hosted anywhere else:
it reads the same database the bot writes, and one service is one thing to run.

It shows real people's names and phone numbers, so everything under /painel/api
needs a session that passed both factors: a password (Argon2id) and a code from
an authenticator app (TOTP, secret encrypted at rest). People get in through a
one-time invite (cli/panel_users.py). Without PANEL_SECRET_KEY the panel is
closed, never open.

Sessions are an opaque random cookie — HttpOnly, Secure, SameSite=Lax,
scoped to /painel — whose SHA-256 is all the database keeps. They end after
PANEL_SESSION_HOURS, or PANEL_IDLE_MINUTES without use, or on "Sair". An
explicitly trusted device instead lasts PANEL_DEVICE_IDLE_DAYS without use,
and any authenticated team member can revoke it.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from psycopg import errors as pg_errors
from pydantic import BaseModel, Field, field_validator

from core import content
from core.config import settings
from core.repositories import automations as automations_repo
from core.repositories import calendar_mirror
from core.repositories import funnel as funnel_repo
from core.repositories import inbox as inbox_repo
from core.repositories import media as media_repo
from core.repositories import panel_auth as auth_repo
from core.repositories import push as push_repo
from core.repositories.connection import get_pool
from core.security import RateLimiter
from core.services import (
    automations,
    billing,
    calendar_sync,
    contract_pdf,
    inbox,
    ocr,
    proposal_pdf,
    proposal_requests,
    push_endpoint,
    wa_templates,
)
from core.services import leads as lead_entry
from core.services import panel_auth as auth
from core.services.funnel import STAGES, Stage
from core.web import panel_admin, panel_media, panel_ocr, panel_uploads

logger = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static" / "painel"
# The only files served from the panel directory, by name. A fixed list rather
# than a StaticFiles mount: nothing a URL can say reaches any other file.
_PAGES = {"app": "index.html", "login": "entrar.html", "invite": "convite.html"}
_ASSETS = {
    "painel.css": "text/css; charset=utf-8",
    "painel.js": "text/javascript; charset=utf-8",
    "auth.css": "text/css; charset=utf-8",
    "auth.js": "text/javascript; charset=utf-8",
    "automacoes.js": "text/javascript; charset=utf-8",
    "inbox.js": "text/javascript; charset=utf-8",
    "attachments.js": "text/javascript; charset=utf-8",
    "billing.js": "text/javascript; charset=utf-8",
    "admin.js": "text/javascript; charset=utf-8",
    "propostas.js": "text/javascript; charset=utf-8",
    "pwa.js": "text/javascript; charset=utf-8",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
}

SESSION_COOKIE = "painel_sessao"
PRE_COOKIE = "painel_pre"      # password accepted, code still owed
_PRE_MINUTES = 5
_MAX_ATTEMPTS = 5              # wrong passwords or codes before the account locks
_LOCK_MINUTES = 15
_INVITE_HOURS = 24

# Failures per client address, across every account: a guesser spraying
# e-mails is stopped here, one hammering a single account by its lockout.
_failures = RateLimiter(max_events=10, window_seconds=300)

_GENERIC = "E-mail ou senha incorretos."
_BAD_CODE = "Código inválido."
_TOO_MANY = "Muitas tentativas. Aguarde alguns minutos e tente de novo."

# What a panel page may load: its own scripts only (no inline, no third party),
# the fonts it uses, and the enrollment QR as an inline image.
_CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; "
    "connect-src 'self'; worker-src 'self'; manifest-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # The invite page's link must never leak to another site as a Referer.
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(self), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def install(app: FastAPI) -> None:
    """Mount the panel and harden every response under /painel."""
    app.include_router(public)
    app.include_router(router)

    @app.middleware("http")
    async def _harden(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/painel"):
            renew = getattr(request.state, "renew_device_cookie", None)
            if renew and response.status_code < 400:
                _set_cookie(response, SESSION_COOKIE, renew,
                            settings.panel_device_idle_days * 24 * 60)
            for name, value in _SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
        return response


# ── Helpers ──────────────────────────────────────────────────────────────────

def _client(request: Request) -> str:
    # Behind the edge proxy, uvicorn's --proxy-headers makes this the real
    # address from X-Forwarded-For; only the proxy can reach the container.
    return request.client.host if request.client else "unknown"


def _private(payload: Any, status_code: int = 200) -> JSONResponse:
    """Personal data: never let a browser or a proxy keep a copy."""
    return JSONResponse(
        jsonable_encoder(payload), status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _set_cookie(response: Response, name: str, value: str, minutes: int) -> None:
    response.set_cookie(
        name, value, max_age=minutes * 60, path="/painel",
        httponly=True, secure=True, samesite="lax",
    )


def _same_origin(request: Request) -> None:
    """Refuse a state change started by another site. The cookie is Lax, not
    Strict, so a link tapped in Telegram opens signed in; Lax still keeps it off
    cross-site requests that are not navigations, and this is the lock on every
    change."""
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="Origem não permitida.")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(status_code=403, detail="Origem não permitida.")


def _require_configured() -> None:
    if not auth.configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Painel desativado: defina PANEL_SECRET_KEY no .env.",
        )


def _guard_ip(request: Request) -> str:
    ip = _client(request)
    if _failures.blocked(ip):
        raise HTTPException(status_code=429, detail=_TOO_MANY)
    return ip


def _page(name: str) -> FileResponse:
    return FileResponse(
        _STATIC / _PAGES[name], media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def _current(request: Request, *, renew: bool = True) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not auth.configured():
        return None
    pool = await get_pool()
    async with pool.connection() as conn:
        found = await auth_repo.session(
            conn, auth.digest(token), mfa=True, idle_minutes=settings.panel_idle_minutes,
            device_idle_days=settings.panel_device_idle_days,
        )
        if found and found["stale"]:
            await auth_repo.touch_session(conn, auth.digest(token))
        if renew and found and found.get("kind") == "device":
            if await auth_repo.renew_device_cookie(conn, auth.digest(token)):
                request.state.renew_device_cookie = token
    return found


async def require_session(request: Request) -> dict:
    _require_configured()
    user = await _current(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Entre novamente.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        _same_origin(request)
    return user


router = APIRouter(prefix="/painel", dependencies=[Depends(require_session)])
public = APIRouter(prefix="/painel")


# ── Pages and assets (no data in them — the data is behind the API) ──────────

@public.get("", include_in_schema=False)
@public.get("/", include_in_schema=False)
async def page(request: Request) -> Response:
    if await _current(request) is None:
        return RedirectResponse("/painel/entrar", status_code=303)
    if request.url.path == "/painel":
        return RedirectResponse("/painel/", status_code=303)
    return _page("app")


@public.get("/api/observability/auth", include_in_schema=False)
async def observability_auth(request: Request) -> Response:
    """Asked by the edge proxy (deploy/edge/Caddyfile) before each request to
    Grafana at /painel/grafana/: the panel session, with its second factor, and
    a seat in the admin room. The answer names the user for Grafana's proxy
    login; anything else never reaches Grafana."""
    from core.repositories import admin_room

    # No renewal of a trusted device's cookie: the proxy does not pass a new
    # cookie back to the browser, so renewing here would waste it.
    user = await _current(request, renew=False)
    if user is None:
        return RedirectResponse("/painel/entrar", status_code=303)
    pool = await get_pool()
    async with pool.connection() as conn:
        if not await admin_room.is_member(conn, user["user_id"]):
            return Response("Só para quem está na sala de administração.", status_code=403,
                            media_type="text/plain; charset=utf-8")
    return Response(status_code=200, headers={"X-Converge-User": f"painel-{user['user_id']}",
                                              "Cache-Control": "no-store"})


@public.get("/entrar", include_in_schema=False)
async def login_page() -> FileResponse:
    return _page("login")


@public.get("/convite", include_in_schema=False)
async def invite_page() -> FileResponse:
    return _page("invite")


@public.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(_STATIC / "manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@public.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    script = (_STATIC / "sw.js").read_text().replace("__APP_VERSION__",
                         json.dumps(f"{settings.app_version}-{_pwa_revision()}"))
    return Response(script, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache"})


@lru_cache(maxsize=1)
def _pwa_revision() -> str:
    """Changes on every UI release even when APP_VERSION keeps its default."""
    files = sorted({*_ASSETS, *_PAGES.values(), "sw.js", "manifest.webmanifest", "favicon.svg",
                    "offline.html", "instalar.html"})
    return hashlib.sha256(b"\0".join((_STATIC / name).read_bytes() for name in files)).hexdigest()[:16]


@public.get("/instalar", include_in_schema=False)
async def install_page() -> FileResponse:
    return FileResponse(_STATIC / "instalar.html", media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@public.get("/offline", include_in_schema=False)
async def offline_page() -> FileResponse:
    return FileResponse(_STATIC / "offline.html", media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@public.get("/favicon.svg", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(
        _STATIC / "favicon.svg", media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@public.get("/assets/{name}", include_in_schema=False)
async def asset(name: str) -> FileResponse:
    media_type = _ASSETS.get(name)
    if media_type is None:
        raise HTTPException(status_code=404)
    # no-cache is revalidate, not "never store": the ETag makes it cheap, and a
    # deploy is never hidden behind yesterday's copy of the script.
    return FileResponse(_STATIC / name, media_type=media_type, headers={"Cache-Control": "no-cache"})


# ── Signing in ───────────────────────────────────────────────────────────────

class Credentials(BaseModel):
    email: str = Field(max_length=160)
    password: str = Field(max_length=256)
    remember_device: bool = False


class Code(BaseModel):
    code: str = Field(max_length=32)


class InviteStart(BaseModel):
    token: str = Field(max_length=128)


class InviteFinish(BaseModel):
    token: str = Field(max_length=128)
    password: str = Field(max_length=256)
    code: str = Field(max_length=32)


async def _fail(conn, request: Request, ip: str, user: dict | None, event: str,
                detail: str) -> HTTPException:
    """Count one failure against the address and, if known, the account."""
    _failures.check(ip)
    locked = False
    if user is not None:
        locked = await auth_repo.record_failure(conn, user["id"], _MAX_ATTEMPTS, _LOCK_MINUTES)
    await auth_repo.log_event(
        conn, "locked" if locked else event,
        user_id=user["id"] if user else None, email=user["email"] if user else None, ip=ip,
    )
    # Committed here: the caller raises next, and the pool rolls back a
    # connection that exits on an exception — the count and the lockout with it.
    await conn.commit()
    return HTTPException(status_code=429 if locked else 401, detail=_TOO_MANY if locked else detail)


async def _open_session(conn, request: Request, response: Response, user_id: int, *,
                        device: bool = False) -> None:
    token = auth.new_token()
    minutes = (settings.panel_device_idle_days * 24 * 60 if device
               else settings.panel_session_hours * 60)
    await auth_repo.create_session(
        conn, auth.digest(token), user_id, mfa=True,
        minutes=minutes, kind="device" if device else "browser",
        device_name=auth.device_name(request.headers.get("user-agent", "")) if device else None,
        ip=_client(request), user_agent=request.headers.get("user-agent"),
    )
    _set_cookie(response, SESSION_COOKIE, token, minutes)
    if device:
        await auth_repo.log_event(conn, "device_signed_in", user_id=user_id, email=None,
                                  ip=_client(request))


@public.post("/api/auth/login")
async def login(request: Request, body: Credentials) -> JSONResponse:
    _require_configured()
    _same_origin(request)
    ip = _guard_ip(request)
    pool = await get_pool()
    async with pool.connection() as conn:
        user = await auth_repo.user_by_email(conn, body.email.strip())
        usable = user if user and user["active"] and user["enrolled_at"] else None
        # Verified even when there is no account, so both take the same time.
        ok = auth.verify_password(usable["password_hash"] if usable else None, body.password)
        if usable and usable["locked"]:
            raise HTTPException(status_code=429, detail=_TOO_MANY)
        if not ok or usable is None:
            raise await _fail(conn, request, ip, usable, "password_fail", _GENERIC)
        if auth.needs_rehash(usable["password_hash"]):
            await auth_repo.set_password_hash(conn, usable["id"], auth.hash_password(body.password))
        token = auth.new_token()
        await auth_repo.create_session(
            conn, auth.digest(token), usable["id"], mfa=False, minutes=_PRE_MINUTES,
            ip=ip, user_agent=request.headers.get("user-agent"),
            kind="device" if body.remember_device else "browser",
        )
        await auth_repo.log_event(conn, "password_ok", user_id=usable["id"],
                                  email=usable["email"], ip=ip)
    response = _private({"next": "code"})
    _set_cookie(response, PRE_COOKIE, token, _PRE_MINUTES)
    return response


@public.post("/api/auth/code")
async def second_factor(request: Request, body: Code) -> JSONResponse:
    """The authenticator code — or, once each, a recovery code."""
    _require_configured()
    _same_origin(request)
    ip = _guard_ip(request)
    pre = request.cookies.get(PRE_COOKIE)
    if not pre:
        raise HTTPException(status_code=401, detail="Entre com sua senha primeiro.")
    pool = await get_pool()
    async with pool.connection() as conn:
        found = await auth_repo.session(conn, auth.digest(pre), mfa=False,
                                        idle_minutes=_PRE_MINUTES)
        if found is None:
            raise HTTPException(status_code=401, detail="Entre com sua senha primeiro.")
        user = await auth_repo.user_by_id(conn, found["user_id"])
        if user is None or user["locked"]:
            raise HTTPException(status_code=429, detail=_TOO_MANY)

        method, recovery_left = None, None
        step = auth.totp_step(auth.decrypt(user["totp_secret_enc"]), body.code)
        if step is not None and await auth_repo.accept_totp_step(conn, user["id"], step):
            method = "totp"
        elif await auth_repo.use_recovery_code(conn, user["id"], auth.recovery_digest(body.code)):
            method = "recovery"
            recovery_left = await auth_repo.recovery_codes_left(conn, user["id"])
        if method is None:
            raise await _fail(conn, request, ip, user, "code_fail", _BAD_CODE)

        await auth_repo.delete_session(conn, auth.digest(pre))
        await auth_repo.clear_failures(conn, user["id"])
        await auth_repo.log_event(conn, f"login_{method}", user_id=user["id"],
                                  email=user["email"], ip=ip)
        response = _private({"ok": True, "recovery_left": recovery_left})
        await _open_session(conn, request, response, user["id"], device=found.get("kind") == "device")
    response.delete_cookie(PRE_COOKIE, path="/painel")
    return response


@public.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    _same_origin(request)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        pool = await get_pool()
        async with pool.connection() as conn:
            await auth_repo.delete_session(conn, auth.digest(token))
    response = _private({"ok": True})
    request.state.renew_device_cookie = None
    response.delete_cookie(SESSION_COOKIE, path="/painel")
    return response


@router.get("/api/devices")
async def devices(user: dict = Depends(require_session)) -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        rows = await auth_repo.devices(conn, user.get("session_id"), settings.panel_device_idle_days)
        with_push = await push_repo.session_ids_with_push(conn)
    return _private({"devices": [{**row, "push": row["id"] in with_push} for row in rows]})


# ── Push notifications (docs/PUSH.md) ────────────────────────────────────────

class PushKeys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class PushSubscription(BaseModel):
    endpoint: str = Field(max_length=push_endpoint.MAX_ENDPOINT)
    keys: PushKeys


class PushEndpoint(BaseModel):
    endpoint: str = Field(max_length=push_endpoint.MAX_ENDPOINT)


def _push_on() -> None:
    if not push_endpoint.push_configured():
        raise HTTPException(status_code=404, detail="Notificações desativadas.")


@router.get("/api/push/key")
async def push_key() -> JSONResponse:
    _push_on()
    return _private({"public_key": settings.push_vapid_public_key})


@router.post("/api/push/subscribe")
async def push_subscribe(body: PushSubscription, user: dict = Depends(require_session)) -> JSONResponse:
    """The browser's subscription, always for the signed-in session."""
    _push_on()
    if not (push_endpoint.valid(body.endpoint)
            and push_endpoint.valid_keys(body.keys.p256dh, body.keys.auth)):
        raise HTTPException(status_code=422, detail="Inscrição inválida.")
    if user.get("session_id") is None:
        raise HTTPException(status_code=401, detail="Entre novamente.")
    pool = await get_pool()
    async with pool.connection() as conn:
        await push_repo.save(conn, user["session_id"], user["user_id"], body.endpoint,
                             body.keys.p256dh, body.keys.auth)
    return _private({"ok": True})


@router.delete("/api/push/subscribe")
async def push_unsubscribe(body: PushEndpoint, user: dict = Depends(require_session)) -> JSONResponse:
    _push_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        removed = await push_repo.delete_own(conn, user.get("session_id") or 0, body.endpoint)
    return _private({"ok": removed})


@router.delete("/api/devices/{session_id}")
async def revoke_device(session_id: int, request: Request,
                        user: dict = Depends(require_session)) -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        if not await auth_repo.revoke_device(conn, session_id):
            raise HTTPException(status_code=404, detail="Aparelho já desconectado.")
        await auth_repo.log_event(conn, "device_revoked", user_id=user["user_id"],
                                  email=user["email"], ip=_client(request))
    current = user.get("session_id") == session_id
    response = _private({"ok": True, "current": current})
    if current:
        request.state.renew_device_cookie = None
        response.delete_cookie(SESSION_COOKIE, path="/painel")
    return response


@public.post("/api/auth/invite/start")
async def invite_start(request: Request, body: InviteStart) -> JSONResponse:
    """A fresh authenticator secret for this invite, as a QR and as text."""
    _require_configured()
    _same_origin(request)
    ip = _guard_ip(request)
    pool = await get_pool()
    async with pool.connection() as conn:
        user = await auth_repo.invited_user(conn, auth.digest(body.token))
        if user is None:
            _failures.check(ip)
            raise HTTPException(status_code=404, detail="Convite inválido ou expirado.")
        secret = auth.new_totp_secret()
        await auth_repo.set_pending_totp(conn, user["id"], auth.encrypt(secret))
    uri = auth.provisioning_uri(secret, user["email"])
    return _private({
        "name": user["name"], "email": user["email"],
        "qr": auth.qr_data_uri(uri), "secret": secret, "min_password": auth.MIN_PASSWORD,
    })


@public.post("/api/auth/invite/finish")
async def invite_finish(request: Request, body: InviteFinish) -> JSONResponse:
    _require_configured()
    _same_origin(request)
    ip = _guard_ip(request)
    token_hash = auth.digest(body.token)
    pool = await get_pool()
    async with pool.connection() as conn:
        user = await auth_repo.invited_user(conn, token_hash)
        if user is None or not user["totp_secret_enc"]:
            _failures.check(ip)
            raise HTTPException(status_code=404, detail="Convite inválido ou expirado.")
        problem = auth.password_problem(body.password, user["email"])
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        step = auth.totp_step(auth.decrypt(user["totp_secret_enc"]), body.code)
        if step is None:
            _failures.check(ip)
            raise HTTPException(
                status_code=400,
                detail="Código não confere. Confira se o app escaneou o QR e use o código atual.",
            )
        codes = auth.new_recovery_codes()
        done = await auth_repo.complete_enrollment(
            conn, token_hash, user["id"], auth.hash_password(body.password), step,
            [auth.recovery_digest(code) for code in codes],
        )
        if not done:
            raise HTTPException(status_code=404, detail="Convite inválido ou expirado.")
        await auth_repo.log_event(conn, "enrolled", user_id=user["id"],
                                  email=user["email"], ip=ip)
        response = _private({"recovery_codes": codes})
        await _open_session(conn, request, response, user["id"])
    return response


# ── The dashboard's data ─────────────────────────────────────────────────────

class LeadUpdate(BaseModel):
    """What a person may change on a card. Only fields actually sent are
    applied, so {"owner_staff_id": null} unassigns and {} changes nothing."""

    stage: Stage | None = None
    owner_staff_id: int | None = None
    interest: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=4000)
    organization: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=30)


@router.get("/api/me")
async def me(user: dict = Depends(require_session)) -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        left = await auth_repo.recovery_codes_left(conn, user["user_id"])
    return _private({"name": user["name"], "email": user["email"], "recovery_left": left})


@router.get("/api/leads")
async def leads() -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        rows = await funnel_repo.list_leads(conn)
        team = await funnel_repo.staff(conn)
    return _private({"leads": rows, "staff": team, "stages": list(STAGES)})


@router.get("/api/leads/{contact_id}")
async def lead(contact_id: int) -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        row = await funnel_repo.lead(conn, contact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Contato não encontrado.")
    return _private(row)


@router.patch("/api/leads/{contact_id}")
async def update_lead(contact_id: int, change: LeadUpdate) -> JSONResponse:
    fields = change.model_dump(exclude_unset=True)
    if "stage" in fields and fields["stage"] is None:
        raise HTTPException(status_code=400, detail="A etapa não pode ficar vazia.")
    pool = await get_pool()
    try:
        async with pool.connection() as conn:
            if not await funnel_repo.update_lead(conn, contact_id, fields):
                raise HTTPException(status_code=404, detail="Contato não encontrado.")
            row = await funnel_repo.lead(conn, contact_id)
    except pg_errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=400, detail="Responsável não existe.") from exc
    return _private(row)


# ── Automations ──────────────────────────────────────────────────────────────

class AutomationUpdate(BaseModel):
    enabled: bool | None = None
    mode: Literal["approve", "auto"] | None = None
    days: int | None = Field(default=None, ge=1, le=365)
    message: str | None = Field(default=None, max_length=automations.MAX_MESSAGE)


class QueueDecision(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    approve: bool


async def _automations_payload(conn) -> dict:
    return {
        "automations": await automations_repo.list_automations(conn),
        "queue": await automations_repo.pending(conn),
        "reasons": await automations_repo.reasons(conn),
        "templates": wa_templates.overview(),
        "whatsapp_ready": settings.whatsapp_configured(),
    }


@router.get("/api/automations")
async def automations_overview() -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        return _private(await _automations_payload(conn))


@router.patch("/api/automations/{automation_id}")
async def automation_update(automation_id: int, change: AutomationUpdate,
                            user: dict = Depends(require_session)) -> JSONResponse:
    fields = change.model_dump(exclude_unset=True)
    if any(value is None for value in fields.values()):
        raise HTTPException(status_code=400, detail="Preencha todos os campos.")
    pool = await get_pool()
    async with pool.connection() as conn:
        automation = await automations_repo.get_automation(conn, automation_id)
        if automation is None:
            raise HTTPException(status_code=404, detail="Automação não encontrada.")
        if "message" in fields:
            if automation["kind"] not in automations.CLIENT_KINDS:
                fields.pop("message")  # only ever tells the team; nothing to edit
            else:
                problem = automations.template_problem(fields["message"])
                if problem:
                    raise HTTPException(status_code=400, detail=problem)
                fields["message"] = fields["message"].strip()
        await automations_repo.update_automation(conn, automation_id, fields, user["email"])
        return _private(await _automations_payload(conn))


@router.get("/api/automations/{automation_id}/preview")
async def automation_preview(automation_id: int) -> JSONResponse:
    """Who this automation would pick right now — nothing is queued or sent."""
    pool = await get_pool()
    async with pool.connection() as conn:
        automation = await automations_repo.get_automation(conn, automation_id)
        if automation is None:
            raise HTTPException(status_code=404, detail="Automação não encontrada.")
        picks = await automations.candidates(conn, automation, limit=50)
    return _private({"people": [
        {"name": pick["name"] or "Sem nome", "reason": automations.reason_for(automation["kind"], pick)}
        for pick in picks
    ]})


@router.post("/api/automations/queue")
async def automation_decide(body: QueueDecision,
                            user: dict = Depends(require_session)) -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        changed = await automations_repo.decide(conn, body.ids, body.approve, user["email"])
        payload = await _automations_payload(conn)
    return _private({**payload, "changed": changed})


# ── Pedidos de proposta ──────────────────────────────────────────────────────

@router.get("/api/proposals")
async def proposals() -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        return _private({"proposals": await proposal_requests.listing(conn)})


async def _proposal(conn, briefing_id: int) -> dict:
    found = await proposal_requests.detail(conn, briefing_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
    return found


@router.get("/api/proposals/{briefing_id}")
async def proposal(briefing_id: int) -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        return _private(await _proposal(conn, briefing_id))


def pdf_disposition(name: str, inline: bool = True) -> str:
    """The file's name for the browser: ASCII fallback, UTF-8 in full (RFC 5987)."""
    plain = name.encode("ascii", "ignore").decode() or "proposta.pdf"
    plain = plain.replace('"', "")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{plain}\"; filename*=UTF-8''{quote(name)}"


@router.get("/api/proposals/{briefing_id}/pdf")
async def proposal_pdf_preview(briefing_id: int) -> Response:
    """The proposal made from the model, to check before sending it."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await _proposal(conn, briefing_id)
        try:
            data, name = await proposal_pdf.generate(conn, briefing_id)
        except proposal_pdf.NoQuote:
            raise HTTPException(status_code=409,
                                detail=content.text("proposal_pdf.sem_orcamento")) from None
        except LookupError:
            raise HTTPException(status_code=404, detail="Pedido não encontrado.") from None
    return Response(data, media_type="application/pdf",
                    headers={"Cache-Control": "no-store",
                             "Content-Disposition": pdf_disposition(name)})


@router.get("/api/proposals/{briefing_id}/contract.pdf")
async def contract_pdf_preview(briefing_id: int) -> Response:
    """The contract made from the model, to check before it goes to be signed."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await _proposal(conn, briefing_id)
        try:
            data, name = await contract_pdf.generate(conn, briefing_id)
        except proposal_pdf.NoQuote:
            raise HTTPException(status_code=409,
                                detail=content.text("proposal_pdf.sem_orcamento")) from None
        except contract_pdf.NoDetails:
            raise HTTPException(status_code=409,
                                detail=content.text("contract_pdf.sem_dados")) from None
        except LookupError:
            raise HTTPException(status_code=404, detail="Pedido não encontrado.") from None
    return Response(data, media_type="application/pdf",
                    headers={"Cache-Control": "no-store",
                             "Content-Disposition": pdf_disposition(name)})


@router.post("/api/proposals/{briefing_id}/sent")
async def proposal_sent(briefing_id: int, user: dict = Depends(require_session)) -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        await _proposal(conn, briefing_id)
        if not await proposal_requests.mark_sent(conn, briefing_id, user):
            raise HTTPException(status_code=409, detail="Este pedido já foi marcado como enviado.")
        return _private(await _proposal(conn, briefing_id))


# The largest value the INTEGER cent columns of proposal_quotes hold.
_QUOTE_MAX = Decimal("21474836.47")


class QuoteEdit(BaseModel):
    """Reais, with at most two decimals: "1500.50", never "1.500,50" nor
    "1.000" (three places, even zeros: it reads as a thousand to a Brazilian)."""

    implantacao: Decimal = Field(gt=0, le=_QUOTE_MAX)
    mensalidade: Decimal = Field(gt=0, le=_QUOTE_MAX)
    # How the setup is paid; none: as it was (pricing.SETUP_TERMS).
    forma: Literal["metade", "parcelado"] | None = None
    observacao: str | None = Field(default=None, max_length=500)

    @field_validator("implantacao", "mensalidade")
    @classmethod
    def _two_places(cls, value: Decimal) -> Decimal:
        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int) or exponent < -2:
            raise ValueError("no máximo duas casas decimais")
        return value


@router.patch("/api/proposals/{briefing_id}/quote")
async def proposal_quote_edit(briefing_id: int, body: QuoteEdit,
                              user: dict = Depends(require_session)) -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        await _proposal(conn, briefing_id)
        note = (body.observacao or "").strip() or None
        if not await proposal_requests.edit_quote(
                conn, briefing_id, user, setup_cents=int(body.implantacao * 100),
                monthly_cents=int(body.mensalidade * 100), note=note, forma=body.forma):
            raise HTTPException(status_code=404, detail="Este pedido não tem orçamento.")
        return _private(await _proposal(conn, briefing_id))


# ── Inbox ────────────────────────────────────────────────────────────────────

Channel = Literal["telegram", "whatsapp", "instagram", "messenger", "email"]
Origin = Literal["anuncio_instagram", "anuncio_facebook", "site", "google", "indicacao", "email",
                 "outro"]


class Reply(BaseModel):
    text: str = Field(min_length=1, max_length=inbox.MAX_REPLY)


class Claim(BaseModel):
    """What the screen showed when the person clicked — required, so a stale
    "Assumir" can never become "Assumir no lugar" by accident."""

    state: Literal["bot", "waiting", "claimed"]
    owner_user_id: int | None


class OriginUpdate(BaseModel):
    origin: Origin | None
    detail: str | None = Field(default=None, max_length=160)


async def _conversation(conn, contact_id: int, user_id: int, after: int | None = None) -> dict:
    found = await inbox_repo.conversation(conn, contact_id, user_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    found.pop("chat_id", None)  # the panel never needs the Telegram id
    is_open = found.get("channel") != "whatsapp" or await inbox_repo.window_open(conn, contact_id)
    found["window_open"] = is_open
    choices = [] if is_open else [{"key": t.key, "label": t.label}
                                  for t in wa_templates.manual_ready()]
    can_reopen = bool(choices)
    found["window_notice"] = None if is_open else content.text(
        "inbox.reopen_notice" if can_reopen else "inbox.window_closed")
    found["can_reopen"] = can_reopen
    found["reopen_choices"] = choices
    found["reopen_choose_label"] = content.text("inbox.reopen_choose")
    found["charge_after_window"] = inbox.template_ready(settings.whatsapp_charge_template)
    found["reopen_label"] = content.text("inbox.reopen_button")
    found["billing_enabled"] = settings.payments_configured()
    lines = await inbox_repo.messages(conn, contact_id, after)
    kept = await media_repo.for_sessions(conn, [line["id"] for line in lines])
    for line in lines:
        if line["id"] in kept:
            line["media"] = kept[line["id"]]
    return {"contact": found, "messages": lines, "ocr_enabled": panel_ocr.enabled(), "uploads_enabled": settings.panel_uploads_enabled}


@router.get("/api/inbox")
async def inbox_list(
    state: Literal["waiting", "mine", "bot", "all", "archived", "blocked"] = Query(
        "all", alias="filter"),
    channel: Channel | None = None,
    user: dict = Depends(require_session),
) -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        rows = await inbox_repo.list_conversations(
            conn, user_id=user["user_id"], state=state, channel=channel
        )
        waiting = await inbox_repo.count_waiting(conn)
    return _private({"conversations": rows, "waiting_total": waiting})


@router.get("/api/inbox/{contact_id}")
async def inbox_conversation(contact_id: int, after: int | None = Query(None, ge=0),
                             user: dict = Depends(require_session)) -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        return _private(await _conversation(conn, contact_id, user["user_id"], after))


async def _act(contact_id: int, user: dict, action) -> JSONResponse:
    """Run one Inbox action and answer with the conversation as it is now. A
    refusal (someone else holds it, the channel said no) is shown as it is."""
    pool = await get_pool()
    async with pool.connection() as conn:
        try:
            await action(conn)
        except inbox.InboxError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return _private(await _conversation(conn, contact_id, user["user_id"]))


@router.api_route("/api/inbox/{contact_id}/media/{media_id}", methods=["GET", "HEAD"])
async def inbox_media(contact_id: int, media_id: int, request: Request) -> Response:
    pool = await get_pool()
    async with pool.connection() as conn:
        return await panel_media.serve(conn, contact_id, media_id, method=request.method,
                                       range_header=request.headers.get("range"),
                                       download=request.query_params.get("download") == "1")


@router.post("/api/inbox/{contact_id}/claim")
async def inbox_claim(contact_id: int, seen: Claim,
                      user: dict = Depends(require_session)) -> JSONResponse:
    return await _act(contact_id, user, lambda conn: inbox.claim(
        conn, contact_id, user, seen_state=seen.state, seen_owner=seen.owner_user_id
    ))


@router.post("/api/inbox/{contact_id}/media/{media_id}/ocr")
async def inbox_ocr(contact_id: int, media_id: int, request: Request) -> JSONResponse:
    if not panel_ocr.enabled():
        raise HTTPException(404, "Leitura de texto desativada.")
    pool = await get_pool()
    try:
        async with panel_ocr.slot():
            async with pool.connection() as conn:
                data = await panel_ocr.read(conn, contact_id, media_id)
            result = await panel_ocr.unless_gone(request, panel_ocr.extract(data))
            # The file may have expired or been removed while the child worked.
            async with pool.connection() as conn:
                if await media_repo.servable(conn, contact_id, media_id) is None:
                    raise ocr.OcrError(404, "Arquivo indisponível.")
    except ocr.OcrError as exc:
        return _private({"detail": exc.detail}, exc.status)
    return _private(result)


@router.post("/api/inbox/{contact_id}/release")
async def inbox_release(contact_id: int, user: dict = Depends(require_session)) -> JSONResponse:
    return await _act(contact_id, user, lambda conn: inbox.release(conn, contact_id, user))


@router.post("/api/inbox/{contact_id}/end")
async def inbox_end(contact_id: int, user: dict = Depends(require_session)) -> JSONResponse:
    return await _act(contact_id, user, lambda conn: inbox.end(conn, contact_id, user))


@router.post("/api/inbox/{contact_id}/block")
async def inbox_block(contact_id: int, user: dict = Depends(require_session)) -> JSONResponse:
    return await _act(contact_id, user, lambda conn: inbox.block(conn, contact_id, user))


@router.post("/api/inbox/{contact_id}/unblock")
async def inbox_unblock(contact_id: int, user: dict = Depends(require_session)) -> JSONResponse:
    return await _act(contact_id, user, lambda conn: inbox.unblock(conn, contact_id, user))


@router.post("/api/leads")
async def add_lead(form: lead_entry.LeadForm, user: dict = Depends(require_session)) -> JSONResponse:
    pool = await get_pool()
    async with pool.connection() as conn:
        try:
            return _private(await lead_entry.add(conn, user, form))
        except inbox.InboxError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


class Reopen(BaseModel):
    template: str | None = Field(default=None, max_length=40)
    # Waits and goes out on the customer's first message after the template.
    reply: str | None = Field(default=None, max_length=inbox.MAX_REPLY)


@router.post("/api/inbox/{contact_id}/reopen")
async def inbox_reopen(contact_id: int, body: Reopen | None = None,
                       user: dict = Depends(require_session)) -> JSONResponse:
    choice = body.template if body else None
    reply = body.reply if body else None
    return await _act(contact_id, user,
                      lambda conn: inbox.reopen(conn, contact_id, user, choice, reply))


@router.post("/api/inbox/{contact_id}/reply")
async def inbox_reply(contact_id: int, body: Reply,
                      user: dict = Depends(require_session)) -> JSONResponse:
    return await _act(contact_id, user, lambda conn: inbox.reply(conn, contact_id, user, body.text))


def _billing_on() -> None:
    if not settings.payments_configured():
        raise HTTPException(status_code=404, detail="Cobrança desligada.")


@router.get("/api/inbox/{contact_id}/billing")
async def inbox_billing(contact_id: int) -> JSONResponse:
    _billing_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        return _private(await billing.summary(conn, contact_id))


@router.post("/api/inbox/{contact_id}/billing")
async def inbox_charge(contact_id: int, form: billing.ChargeForm,
                       user: dict = Depends(require_session)) -> JSONResponse:
    _billing_on()
    return await _act(contact_id, user, lambda conn: billing.create(conn, contact_id, user, form))


@router.post("/api/inbox/{contact_id}/billing/{charge_id}/resend")
async def inbox_charge_resend(contact_id: int, charge_id: int,
                              user: dict = Depends(require_session)) -> JSONResponse:
    _billing_on()
    return await _act(contact_id, user,
                      lambda conn: billing.resend(conn, contact_id, charge_id, user))


@router.post("/api/inbox/{contact_id}/billing/{charge_id}/cancel")
async def inbox_charge_cancel(contact_id: int, charge_id: int,
                              user: dict = Depends(require_session)) -> JSONResponse:
    _billing_on()
    return await _act(contact_id, user,
                      lambda conn: billing.cancel(conn, contact_id, charge_id, user))


@router.patch("/api/contacts/{contact_id}/origin")
async def contact_origin(contact_id: int, change: OriginUpdate,
                         user: dict = Depends(require_session)) -> JSONResponse:
    detail = (change.detail or "").strip() or None
    pool = await get_pool()
    async with pool.connection() as conn:
        if not await inbox_repo.set_origin(conn, contact_id, change.origin, detail, user["user_id"]):
            raise HTTPException(status_code=404, detail="Contato não encontrado.")
        return _private(await _conversation(conn, contact_id, user["user_id"]))


@router.get("/api/agenda")
async def agenda(days_back: int = 14, days_ahead: int = 30) -> JSONResponse:
    days_back = max(0, min(days_back, 90))
    days_ahead = max(1, min(days_ahead, 90))
    now = datetime.now().astimezone()
    start, end = now - timedelta(days=days_back), now + timedelta(days=days_ahead)
    pool = await get_pool()
    async with pool.connection() as conn:
        rows = await funnel_repo.appointments_between(conn, start, end)
        team = await funnel_repo.staff(conn)
        # The team's calendars as mirrored: what was booked by hand shows too.
        events = await calendar_mirror.events_between(conn, start, end)
        sync = await calendar_sync.status(conn)
    return _private({"appointments": rows, "staff": team, "events": events, "sync": sync})


@router.post("/api/agenda/sync")
async def agenda_sync() -> JSONResponse:
    """"Atualizar agenda": the sync runs now; the page polls until `done`."""
    return _private({"requested_at": calendar_sync.request_sync()})


router.include_router(panel_admin.router(require_session, lambda: get_pool(), _private))

router.include_router(panel_uploads.router(require_session, lambda: get_pool(), _private, SESSION_COOKIE))
