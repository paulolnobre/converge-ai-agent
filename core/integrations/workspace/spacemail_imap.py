"""Reading new mail from the Spacemail INBOX over IMAP, to tell the team.

Read only: the folder is opened read-only and every fetch is BODY.PEEK, so
nothing is marked as read or changed. Nothing here logs an address or a
password; failures are raised `from None` as WorkspaceError.
"""
from __future__ import annotations

import html
import imaplib
import re
import ssl
import time
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr

from core.config import settings
from core.integrations.workspace.errors import (
    WorkspaceAuthError,
    WorkspaceError,
    WorkspaceNotConfigured,
    WorkspaceTransient,
)

_TIMEOUT_SECONDS = 20
_FETCH_BYTES = 20000     # enough for the headers and the start of the text
_MAX_PER_PASS = 50       # newest ones, if a lot piled up
_READ_BUDGET_SECONDS = 45  # past this, stop and leave the rest for the next pass
_PREVIEW_CHARS = 200

_AUTOMATIC_SENDERS = re.compile(
    r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?)([+._-].*)?$", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]+")


@dataclass(frozen=True)
class IncomingMail:
    uid: int
    sender_name: str
    sender_email: str
    subject: str
    preview: str
    automated: bool


@dataclass(frozen=True)
class Fetched:
    uidvalidity: int
    max_uid: int
    mails: list[IncomingMail]
    skipped: int = 0     # newer than the cap, or a message the server would not give


def clean(text: str, limit: int) -> str:
    """One line, no control characters, cut at `limit`."""
    return _CONTROL.sub(" ", text).strip()[:limit]


def is_automated(message: EmailMessage) -> bool:
    if message["List-Unsubscribe"] or message["X-Auto-Response-Suppress"]:
        return True
    if (message["Precedence"] or "").strip().lower() in ("bulk", "list", "junk"):
        return True
    if (message["Auto-Submitted"] or "no").strip().lower() != "no":
        return True
    local = parseaddr(str(message["From"] or ""))[1].partition("@")[0]
    return bool(_AUTOMATIC_SENDERS.match(local))


def _text_of(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError, UnicodeError):
        return ""
    if part.get_content_type() == "text/html":
        text = html.unescape(re.sub(r"(?is)<(script|style).*?(</\1>|$)|<[^>]*>", " ", text))
    return text


def parse(uid: int, raw: bytes) -> IncomingMail:
    message = message_from_bytes(raw, policy=policy.default)
    name, address = parseaddr(str(message["From"] or ""))
    return IncomingMail(
        uid=uid, sender_name=clean(name, 80), sender_email=address.strip().lower(),
        subject=clean(str(message["Subject"] or ""), 150),
        preview=clean(_text_of(message), _PREVIEW_CHARS),
        automated=is_automated(message),
    )


def _uids(data) -> list[int]:
    return [int(x) for x in (data[0] or b"").split()]


def _read(state: tuple[int, int] | None) -> Fetched:
    with imaplib.IMAP4_SSL(settings.spacemail_imap_host, settings.spacemail_imap_port,
                           ssl_context=ssl.create_default_context(),
                           timeout=_TIMEOUT_SECONDS) as imap:
        imap.login(settings.spacemail_username, settings.spacemail_password)
        imap.select("INBOX", readonly=True)
        validity = int(imap.response("UIDVALIDITY")[1][0])
        if state is None or state[0] != validity:
            # First look, or the server renumbered: only say where it stands.
            _, data = imap.uid("SEARCH", "ALL")
            return Fetched(validity, max(_uids(data), default=0), [])
        last_uid = state[1]
        _, data = imap.uid("SEARCH", f"UID {last_uid + 1}:*")
        new = sorted(u for u in _uids(data) if u > last_uid)
        wanted = new[-_MAX_PER_PASS:]
        mails, skipped = [], len(new) - len(wanted)
        deadline = time.monotonic() + _READ_BUDGET_SECONDS
        for index, uid in enumerate(wanted):
            if time.monotonic() > deadline:
                # The point stops before this one: the rest come next pass.
                return Fetched(validity, wanted[index - 1] if index else last_uid,
                               mails, skipped)
            status, parts = imap.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{_FETCH_BYTES}>)")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), b"")
            if status != "OK" or not raw:
                skipped += 1    # one message the server will not give must not block the rest
                continue
            mails.append(parse(uid, raw))
        return Fetched(validity, max(new, default=last_uid), mails, skipped)


def fetch_since(state: tuple[int, int] | None) -> Fetched:
    """Blocking; call it in a thread. `state` is (uidvalidity, last_uid). With
    none, or after the server renumbered, only report where the mailbox stands
    now (no mails). Otherwise the mails after `last_uid`."""
    if not settings.spacemail_enabled:
        raise WorkspaceNotConfigured("Spacemail is not configured.")
    host = settings.spacemail_imap_host
    try:
        return _read(state)
    except imaplib.IMAP4.error as exc:
        if "AUTH" in str(exc).upper() or "LOGIN" in str(exc).upper():
            raise WorkspaceAuthError(f"IMAP {host}: login refused.") from None
        raise WorkspaceError(f"IMAP {host}: the server refused a command.") from None
    except (OSError, ValueError, IndexError, TypeError):
        raise WorkspaceTransient(f"IMAP {host}: connection or reply failed.") from None
