"""Finding and sending the files people ask the desk for.

Phase 3. The rule that shapes this module: a file leaves the organization only
from `SHAREABLE_FOLDER_ID`. Everything else needs a person to say yes, because a
drive search for "contrato" will happily find someone else's contract.
"""
from __future__ import annotations

import logging

from core.config import settings
from core.integrations import workspace
from core.integrations.workspace import Attachment, FileRef, OutgoingMail
from core.integrations.workspace.errors import audit_detail
from core.repositories import audit

logger = logging.getLogger(__name__)


async def search(term: str, folder_id: str | None = None, limit: int = 10
                 ) -> list[FileRef]:
    """Find files by name inside a folder the agent is allowed to look in."""
    return await workspace.files().list_files(
        folder_id=folder_id or settings.documents_root_folder_id,
        name_contains=term,
        limit=limit,
    )


def is_shareable(file: FileRef) -> bool:
    """Whether this file may be sent without a human approving it.

    A file with no parent, or a parent that is not the shareable folder, is not
    shareable. Defaulting to yes here is how a private document gets e-mailed to
    someone who asked nicely.
    """
    if not settings.shareable_folder_id:
        return False
    return file.parent_id == settings.shareable_folder_id


async def send_file(conn, *, file: FileRef, to: list[str], subject: str,
                    body: str, mailbox: str | None = None,
                    contact_id: int | None = None, approved: bool = False) -> bool:
    """E-mail a file out. Refuses anything outside the shareable folder.

    `approved` is the explicit human override, and it is recorded in the audit
    row so "who let this out?" has an answer.
    """
    if not (approved or is_shareable(file)):
        logger.warning("Refusing to send %s: outside the shareable folder.", file.id)
        return False
    if file.size_bytes and file.size_bytes > settings.max_attachment_bytes:
        logger.warning("Refusing to send %s: larger than the attachment limit.", file.id)
        return False

    # Named by the step that fails: the drive while downloading, the sender after.
    provider = workspace.configuration_report()["capabilities"]["files"]
    try:
        data = await workspace.files().download(file.id)
        provider = workspace.mail_sender_provider()
        await workspace.mail_sender().send_message(
            mailbox or workspace.sender_mailbox(),
            OutgoingMail(
                to=to,
                subject=subject,
                body_text=body,
                attachments=[
                    Attachment(
                        filename=file.name,
                        content_type=file.mime_type or "application/octet-stream",
                        data=data,
                    )
                ],
            ),
        )
    except workspace.WorkspaceError as exc:
        logger.error("Could not send file %s: %s", file.id, exc)
        await audit.record(
            conn, provider=provider, capability="mail", action="send_file",
            target=file.id, contact_id=contact_id, succeeded=False, detail=audit_detail(exc),
        )
        return False

    await audit.record(
        conn, provider=provider, capability="mail", action="send_file",
        target=file.id, contact_id=contact_id, succeeded=True,
        detail="approved by a person" if approved else "shareable folder",
    )
    return True


async def file_incoming(conn, *, name: str, data: bytes, mime_type: str,
                        folder_id: str | None = None,
                        contact_id: int | None = None) -> FileRef | None:
    """Put a document someone sent into the drive, where the team will find it."""
    provider = workspace.configuration_report()["capabilities"]["files"]
    try:
        stored = await workspace.files().upload(
            name, data, mime_type,
            folder_id=folder_id or settings.documents_root_folder_id,
        )
    except workspace.WorkspaceError as exc:
        # No id yet, and the name is the customer's own ("Contrato — Maria…").
        logger.error("Upload failed: %s", exc)
        await audit.record(
            conn, provider=provider, capability="files", action="upload",
            target=None, contact_id=contact_id, succeeded=False, detail=audit_detail(exc),
        )
        return None

    await audit.record(
        conn, provider=provider, capability="files", action="upload",
        target=stored.id, contact_id=contact_id, succeeded=True,
    )
    return stored
