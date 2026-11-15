"""Access tokens for Google Workspace and Microsoft 365.

Three grants, because the three deployments a client actually has are not the
same shape:

- Google service account with domain-wide delegation — the tenant's admin
  authorizes the app once and the agent impersonates a mailbox/calendar. No
  user ever clicks a consent screen, which is what a headless bot needs.
- Google installed-app refresh token — a single account (a shared
  `obra@` or `recepcao@` address) authorized by hand with `cli/authorize_google.py`.
  This is the small-office path: no Workspace admin required.
- Microsoft client credentials — an Entra ID app registration with application
  permissions. Graph exposes every mailbox and drive in the tenant, so the
  configured `MS_*_UPN` settings are what narrow it back down.

Tokens are cached in memory until shortly before they expire. They are never
written to the database or to a log line.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from core.config import settings
from core.integrations.workspace.errors import WorkspaceAuthError, WorkspaceNotConfigured

logger = logging.getLogger(__name__)

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_EXPIRY_MARGIN = 120  # refresh this many seconds before the provider expires it

_client: httpx.AsyncClient | None = None
_cache: dict[str, tuple[str, float]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


def _cached(key: str) -> str | None:
    entry = _cache.get(key)
    if entry and entry[1] - _EXPIRY_MARGIN > time.time():
        return entry[0]
    return None


def _store(key: str, token: str, expires_in: float) -> str:
    _cache[key] = (token, time.time() + float(expires_in))
    return token


def _lock(key: str) -> asyncio.Lock:
    """One in-flight refresh per credential, so a burst mints one token."""
    return _locks.setdefault(key, asyncio.Lock())


def google_scopes() -> list[str]:
    return [scope for scope in settings.google_scopes.split(",") if scope.strip()]


async def _google_service_account_token(subject: str | None) -> str:
    """Mint a delegated token from the service-account key file.

    `google-auth` does the RS256 signing and the token exchange; both are
    blocking, so the whole refresh runs in a worker thread.
    """
    if not settings.google_credentials_path:
        raise WorkspaceNotConfigured("GOOGLE_CREDENTIALS_PATH is not set.")

    def _mint() -> tuple[str, float]:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            settings.google_credentials_path, scopes=google_scopes()
        )
        if subject:
            credentials = credentials.with_subject(subject)
        credentials.refresh(Request())
        expiry = credentials.expiry
        ttl = 3600.0
        if expiry is not None:
            ttl = max(60.0, expiry.timestamp() - time.time())
        if not credentials.token:
            raise WorkspaceAuthError("Google returned an empty service-account token.")
        return credentials.token, ttl

    try:
        token, ttl = await asyncio.to_thread(_mint)
    except WorkspaceAuthError:
        raise
    except FileNotFoundError as exc:
        raise WorkspaceNotConfigured(
            f"Google credentials file not found: {settings.google_credentials_path}"
        ) from exc
    except Exception as exc:  # google-auth raises a wide family of errors
        # google-auth's text names the impersonated user; keep only the kind.
        raise WorkspaceAuthError(
            f"Google service-account refresh failed ({type(exc).__name__})"
        ) from None
    return _store(f"google:sa:{subject or '-'}", token, ttl)


async def _google_refresh_token() -> str:
    if not (
        settings.google_client_id
        and settings.google_client_secret
        and settings.google_refresh_token
    ):
        raise WorkspaceNotConfigured(
            "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN are required "
            "when GOOGLE_AUTH_MODE=refresh_token."
        )
    response = await _http().post(
        _GOOGLE_TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": settings.google_refresh_token,
            "grant_type": "refresh_token",
        },
    )
    if response.status_code != 200:
        # The body carries the client secret back in some error shapes; only the
        # status and Google's short error code are safe to log.
        raise WorkspaceAuthError(
            f"Google token endpoint returned {response.status_code} "
            f"({response.json().get('error', 'unknown') if response.text else 'no body'})"
        )
    body = response.json()
    return _store("google:user", body["access_token"], body.get("expires_in", 3600))


async def google_access_token(subject: str | None = None) -> str:
    """A bearer token for the Google REST APIs.

    `subject` is the mailbox or calendar owner to impersonate; it is only
    meaningful under domain-wide delegation and is ignored otherwise.
    """
    if settings.google_auth_mode == "service_account":
        key = f"google:sa:{subject or settings.google_delegated_subject or '-'}"
        async with _lock(key):
            cached = _cached(key)
            if cached:
                return cached
            return await _google_service_account_token(
                subject or settings.google_delegated_subject
            )

    async with _lock("google:user"):
        cached = _cached("google:user")
        if cached:
            return cached
        return await _google_refresh_token()


async def microsoft_access_token() -> str:
    """An app-only Graph token for the configured Entra ID tenant."""
    if not (
        settings.ms_tenant_id and settings.ms_client_id and settings.ms_client_secret
    ):
        raise WorkspaceNotConfigured(
            "MS_TENANT_ID, MS_CLIENT_ID and MS_CLIENT_SECRET are required for "
            "Microsoft 365 access."
        )
    async with _lock("microsoft"):
        cached = _cached("microsoft")
        if cached:
            return cached
        url = (
            f"https://login.microsoftonline.com/{settings.ms_tenant_id}"
            "/oauth2/v2.0/token"
        )
        response = await _http().post(
            url,
            data={
                "client_id": settings.ms_client_id,
                "client_secret": settings.ms_client_secret,
                "scope": settings.ms_scope,
                "grant_type": "client_credentials",
            },
        )
        if response.status_code != 200:
            raise WorkspaceAuthError(
                f"Microsoft token endpoint returned {response.status_code}"
            )
        body = response.json()
        return _store("microsoft", body["access_token"], body.get("expires_in", 3600))


def forget_tokens() -> None:
    """Drop cached tokens (used by tests and by credential rotation)."""
    _cache.clear()


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
