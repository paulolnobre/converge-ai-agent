"""Spacemail (the mail host of getconverge.me) as a sender, over SMTP.

Send only: no reading, no marking as read. The mailbox is the one in
SPACEMAIL_USERNAME and the message always leaves from it. Nothing here logs an
address or a password; a failure names the host and the SMTP status, and is
raised `from None` so the original error (which lists recipients) never
reaches a traceback.
"""
from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from core.config import settings
from core.integrations.workspace.errors import (
    WorkspaceAuthError,
    WorkspaceError,
    WorkspaceNotConfigured,
    WorkspaceTransient,
)
from core.integrations.workspace.models import OutgoingMail

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 20
# One bare address: no spaces, no line breaks, one @, no angle brackets or commas.
_ADDRESS = re.compile(r"[^\s<>,;\"@]+@[^\s<>,;\"@]+\Z")


def build(message: OutgoingMail, sender: str) -> EmailMessage:
    """The RFC 822 message. Bcc is not a header: it only widens the envelope."""
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = ", ".join(message.to)
    if message.cc:
        mail["Cc"] = ", ".join(message.cc)
    mail["Subject"] = message.subject
    mail["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    if message.reply_to_message_id:
        mail["In-Reply-To"] = message.reply_to_message_id
        mail["References"] = message.reply_to_message_id
    mail.set_content(message.body_text)
    for attachment in message.attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        mail.add_attachment(attachment.data, maintype=maintype or "application",
                            subtype=subtype or "octet-stream", filename=attachment.filename)
    return mail


def _send(message: OutgoingMail, sender: str, recipients: list[str]) -> tuple[str, int]:
    """Build the message and hand it to the server, all in one worker thread
    (the encoding of a large attachment is work too). Returns the Message-ID
    and how many recipients the server refused while taking the rest."""
    mail = build(message, sender)
    host, port = settings.spacemail_host, settings.spacemail_port
    with smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT_SECONDS,
                          context=ssl.create_default_context()) as smtp:
        smtp.login(settings.spacemail_username, settings.spacemail_password)
        refused = smtp.send_message(mail, to_addrs=recipients)
    return str(mail["Message-ID"]), len(refused or {})


class SpacemailMailer:
    async def send_message(self, mailbox: str, message: OutgoingMail) -> str:
        """Send one e-mail from the configured mailbox; returns its Message-ID.
        `mailbox` is accepted for the shared sender interface and ignored: the
        sender is always the Spacemail login."""
        if not settings.spacemail_enabled:
            raise WorkspaceNotConfigured("Spacemail is not configured.")
        host = settings.spacemail_host
        recipients = [*message.to, *message.cc, *message.bcc]
        if not recipients or not all(_ADDRESS.match(a) for a in recipients):
            raise WorkspaceError(f"SMTP {host}: a recipient address is not valid.")
        try:
            message_id, refused = await asyncio.to_thread(
                _send, message, settings.spacemail_username, recipients)
        except smtplib.SMTPAuthenticationError:
            raise WorkspaceAuthError(f"SMTP {host}: login refused.", status=535) from None
        except smtplib.SMTPResponseException as exc:
            status = exc.smtp_code
            raise WorkspaceError(f"SMTP {host}: refused with status {status}.",
                                 status=status) from None
        except smtplib.SMTPRecipientsRefused:
            raise WorkspaceError(f"SMTP {host}: every recipient was refused.") from None
        except (smtplib.SMTPException, OSError):
            raise WorkspaceTransient(f"SMTP {host}: connection failed.") from None
        except (ValueError, TypeError):
            raise WorkspaceError(f"SMTP {host}: the message could not be built.") from None
        if refused:
            # The others were delivered; repeating the send would duplicate it.
            logger.warning("Spacemail refused %d of %d recipients.", refused, len(recipients))
        return message_id
