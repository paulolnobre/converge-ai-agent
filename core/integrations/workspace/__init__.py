"""One façade over Google Workspace and Microsoft 365.

Call `calendar()`, `mail()`, `files()` or `sheets()`; which suite answers is a
configuration question. Providers are resolved per capability, because the split
tenant is real: a builder whose e-mail is on Microsoft 365 but whose cost
spreadsheets everyone actually edits live in Google Sheets.

Instances are cached per provider — they hold nothing but a default UPN/subject,
and the HTTP client and tokens underneath them are already shared.
"""
from __future__ import annotations

import logging

from core.config import settings
from core.integrations.workspace import google as google_module
from core.integrations.workspace import microsoft as microsoft_module
from core.integrations.workspace import oauth
from core.integrations.workspace.base import (
    CalendarPort,
    FilesPort,
    MailPort,
    MailSenderPort,
    SheetsPort,
)
from core.integrations.workspace.errors import (
    WorkspaceAuthError,
    WorkspaceError,
    WorkspaceNotConfigured,
    WorkspaceRateLimited,
)
from core.integrations.workspace.google import GoogleWorkspace
from core.integrations.workspace.microsoft import MicrosoftWorkspace
from core.integrations.workspace.models import (
    Attachment,
    Attendee,
    CalendarEvent,
    FileRef,
    MailAddress,
    MailMessage,
    OutgoingMail,
    Provider,
    SheetValues,
    TimeSlot,
)
from core.integrations.workspace.spacemail import SpacemailMailer

logger = logging.getLogger(__name__)

__all__ = [
    "Attachment",
    "Attendee",
    "CalendarEvent",
    "CalendarPort",
    "FileRef",
    "FilesPort",
    "MailAddress",
    "MailMessage",
    "MailPort",
    "MailSenderPort",
    "OutgoingMail",
    "Provider",
    "SheetValues",
    "SheetsPort",
    "TimeSlot",
    "WorkspaceAuthError",
    "WorkspaceError",
    "WorkspaceNotConfigured",
    "WorkspaceRateLimited",
    "calendar",
    "close",
    "configuration_report",
    "files",
    "mail",
    "mail_sender",
    "mail_sender_provider",
    "sender_mailbox",
    "sheets",
]

_instances: dict[str, GoogleWorkspace | MicrosoftWorkspace] = {}


def _provider_for(capability: str) -> Provider:
    override = getattr(settings, f"{capability}_provider", None) or ""
    chosen = override.strip() or settings.workspace_provider
    if chosen not in ("google", "microsoft"):
        raise WorkspaceNotConfigured(
            f"Unknown workspace provider {chosen!r} for {capability}."
        )
    return chosen  # type: ignore[return-value]


def _client(capability: str) -> GoogleWorkspace | MicrosoftWorkspace:
    provider = _provider_for(capability)
    if provider not in _instances:
        _instances[provider] = (
            GoogleWorkspace() if provider == "google" else MicrosoftWorkspace()
        )
    return _instances[provider]


def calendar() -> CalendarPort:
    return _client("calendar")


def mail() -> MailPort:
    return _client("mail")


_spacemail = SpacemailMailer()


def mail_sender() -> MailSenderPort:
    """Who sends the e-mail the agent starts (reminders, documents): the
    Spacemail mailbox when it is configured, otherwise the suite's mail."""
    return _spacemail if settings.spacemail_enabled else mail()


def mail_sender_provider() -> str:
    """The name the audit trail records for `mail_sender()`."""
    return "spacemail" if settings.spacemail_enabled else _provider_for("mail")


def sender_mailbox() -> str:
    """The address `mail_sender()` sends from, empty when there is none."""
    if settings.spacemail_enabled:
        return settings.spacemail_username
    return settings.reception_mailbox or ""


def files() -> FilesPort:
    return _client("files")


def sheets() -> SheetsPort:
    return _client("sheets")


def configuration_report() -> dict:
    """What `/ready` shows: which suite serves what, and whether it could.

    Only presence is reported. A credential is never echoed, not even truncated.
    """
    google_ready = bool(
        settings.google_credentials_path
        if settings.google_auth_mode == "service_account"
        else settings.google_refresh_token and settings.google_client_id
    )
    microsoft_ready = bool(
        settings.ms_tenant_id and settings.ms_client_id and settings.ms_client_secret
    )
    ready = {"google": google_ready, "microsoft": microsoft_ready}
    capabilities = {
        capability: _provider_for(capability)
        for capability in ("calendar", "mail", "files", "sheets")
    }
    missing = sorted(
        {provider for provider in capabilities.values() if not ready[provider]}
    )
    return {
        "status": "configured" if not missing else "incomplete",
        "capabilities": capabilities,
        "providers": {
            "google": "configured" if google_ready else "missing credentials",
            "microsoft": "configured" if microsoft_ready else "missing credentials",
        },
        "missing": missing,
    }


def reset() -> None:
    """Drop cached clients and tokens (tests, credential rotation)."""
    _instances.clear()
    oauth.forget_tokens()


async def close() -> None:
    """Release every HTTP client this layer opened, on replica shutdown."""
    await google_module.close()
    await microsoft_module.close()
    await oauth.close()
    _instances.clear()
