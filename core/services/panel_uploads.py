"""Staff uploads: encrypted storage, scoped access and durable send attempts.

A committed `sending` mark precedes any external send. Repeating the nonce
never repeats a send, even after cancellation or a lost response. Only a
confirmed channel result becomes `sent`; abandoned attempts remain uncertain.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from core.config import settings
from core.integrations import telegram, whatsapp
from core.repositories import admin_room, contacts, handoffs, media, panel_auth
from core.sealed_fields import field, write
from core.services import inbox, media_store, push
from core.services.upload_types import Upload

logger = logging.getLogger(__name__)


class UploadError(inbox.InboxError):
    pass


async def authorize(conn, user: dict, token_hash: str, contact_id: int | None):
    if contact_id is None:
        try:
            await admin_room.require_member(conn, user["user_id"])
        except admin_room.NotMember:
            raise UploadError(403, "Você não participa do grupo administrativo.") from None
        target = None
    else:
        target = await inbox._contact(conn, contact_id)
        if target.channel not in ("whatsapp", "telegram"):
            raise UploadError(501, "Canal ainda não conectado.")
        if not await handoffs.lock_for_reply(conn, target.chat_id, user["user_id"]):
            raise UploadError(409, "Assuma a conversa para enviar anexos.")
        if not await inbox._window_open(conn, target):
            raise UploadError(
                409, "A janela do WhatsApp está fechada. Aguarde uma nova mensagem do cliente."
            )
    # Check AFTER any wait for the conversation lock; lock account/session until commit.
    await require_live_session(conn, user, token_hash)
    return target


async def require_live_session(conn, user: dict, token_hash: str) -> None:
    """The session that sent the request is still signed in, with MFA, and its
    account active — read again after any wait, and held until commit."""
    live = await panel_auth.session(
        conn,
        token_hash,
        mfa=True,
        idle_minutes=settings.panel_idle_minutes,
        device_idle_days=settings.panel_device_idle_days,
    )
    if not live or live["user_id"] != user["user_id"]:
        raise UploadError(401, "Entre novamente.")
    cur = await conn.execute(
        "SELECT u.id FROM panel_users u JOIN panel_sessions s ON s.user_id=u.id WHERE u.id=%s AND s.token_hash=%s AND u.active AND s.mfa FOR SHARE OF u,s",
        (user["user_id"], token_hash),
    )
    if not await cur.fetchone():
        raise UploadError(401, "Entre novamente.")


def fingerprint(data: bytes, kind: str, filename: str, caption: str) -> str:
    return hmac.new(
        base64.b64decode(settings.data_index_key),
        kind.encode() + b"\0" + filename.encode() + b"\0" + caption.encode() + b"\0" + data,
        hashlib.sha256,
    ).hexdigest()


def _result(row: dict, duplicate: bool) -> dict:
    state = row["delivery_state"]
    return {
        "ok": state == "sent",
        "media_id": row["id"],
        "delivery_state": state,
        "duplicate": duplicate,
        "notice": None
        if state == "sent"
        else (
            "O canal recusou o envio. Escolha o arquivo novamente para tentar."
            if state == "failed"
            else "O envio ainda não foi confirmado. Confira com o destinatário antes de tentar outro envio."
        ),
    }


async def _existing(conn, user_id: int, nonce: UUID) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id,contact_id,request_fingerprint,delivery_state FROM media WHERE author_user_id=%s AND client_nonce=%s",
            (user_id, nonce),
        )
        return await cur.fetchone()


async def deliver(
    pool,
    user: dict,
    token_hash: str,
    contact_id: int | None,
    nonce: UUID,
    request_hash: str,
    upload: Upload,
    caption: str,
) -> dict:
    key = uuid4()
    durable = False
    try:
        async with pool.connection() as conn:
            target = await authorize(conn, user, token_hash, contact_id)
            # Serialize this actor+nonce, including races with another upload request.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    int.from_bytes(
                        hashlib.sha256(f"{user['user_id']}:{nonce}".encode()).digest()[:8],
                        "big",
                        signed=True,
                    ),
                ),
            )
            previous = await _existing(conn, user["user_id"], nonce)
            if previous:
                if (
                    previous["contact_id"] != contact_id
                    or previous["request_fingerprint"] != request_hash
                ):
                    raise UploadError(409, "Este identificador já pertence a outro envio.")
                return _result(previous, True)
            if (
                target
                and target.channel == "whatsapp"
                and upload.mime == "application/octet-stream"
            ):
                raise UploadError(
                    422, "Para WhatsApp, escolha PDF, TXT, Word, Excel ou PowerPoint."
                )
            if upload.kind == "audio" and caption:
                raise UploadError(
                    422, "Envie o áudio sem legenda. O texto pode ser enviado separadamente."
                )
            await conn.execute("SELECT pg_advisory_xact_lock(7411305)")
            if not media_store.has_room(len(upload.data) + 64, await media.stored_total(conn)):
                raise UploadError(507, "Sem espaço para guardar o anexo. Tente mais tarde.")
            count = await asyncio.to_thread(media_store.write, key, upload.data)
            label = {"image": "Foto", "document": "Arquivo", "audio": "Áudio"}[upload.kind]
            line = label + (" · " + caption if caption else "")
            admin_id = session_id = None
            if contact_id is None:
                admin_id = await admin_room.insert(
                    conn, line, sender_id=user["user_id"], kind="person", nonce=nonce
                )
                if admin_id is None:
                    raise UploadError(409, "Este identificador já pertence a outra mensagem.")
            else:
                assert target is not None
                session_id = await contacts.record_message(
                    conn,
                    contact_id,
                    target.chat_id,
                    "assistant",
                    line,
                    "relay",
                    author_user_id=user["user_id"],
                )
            sealed = write(field("media", "filename"), upload.filename)
            now = datetime.now(UTC)
            expiry = (
                now + timedelta(hours=48) if upload.kind == "audio" else now + timedelta(days=90)
            )
            cur = await conn.execute(
                "INSERT INTO media (contact_id,session_id,chat_id,source_id,kind,mime,declared_bytes,stored_bytes,filename_sealed,storage_key,status,stored_at,expires_at,admin_message_id,author_user_id,client_nonce,request_fingerprint,delivery_state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'stored',%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (
                    contact_id,
                    session_id,
                    target.chat_id if target else "admin",
                    f"panel:{user['user_id']}:{nonce}",
                    upload.kind,
                    upload.mime,
                    len(upload.data),
                    count,
                    sealed["filename_sealed"],
                    key,
                    now,
                    expiry,
                    admin_id,
                    user["user_id"],
                    nonce,
                    request_hash,
                    "sending" if target else "sent",
                ),
            )
            row = await cur.fetchone()
            assert row
            media_id = row[0]
            subscriptions = (
                await admin_room.subscriptions(conn, except_user=user["user_id"])
                if contact_id is None
                else []
            )
        durable = True
    except BaseException:
        # Cancellation of a threaded write can leave an encrypted orphan; the existing sweep removes it.
        if not durable:
            await asyncio.to_thread(media_store.delete, key)
        raise
    if contact_id is None:
        push.schedule(
            subscriptions,
            title="💬 Novo anexo na Administração",
            url="/painel/#admin",
            tag=f"admin:{admin_id}",
        )
        return {
            "ok": True,
            "media_id": media_id,
            "delivery_state": "sent",
            "duplicate": False,
            "notice": None,
        }
    state = "uncertain"
    try:
        async with pool.connection() as conn:
            target = await authorize(conn, user, token_hash, contact_id)
            assert target is not None and target.chat_id is not None
            # The owner lock is intentionally held through the bounded send, as for text replies.
            async with asyncio.timeout(55):
                if target.channel == "whatsapp":
                    state = await whatsapp.send_attachment(
                        target.chat_id.removeprefix("wa:"),
                        upload.data,
                        upload.kind,
                        upload.mime,
                        upload.filename,
                        caption,
                    )
                else:
                    state = await telegram.send_attachment(
                        target.chat_id,
                        upload.data,
                        upload.kind,
                        upload.mime,
                        upload.filename,
                        caption,
                    )
            state = {
                "ok": "sent",
                "window_closed": "failed",
                "failed": "failed",
                "uncertain": "uncertain",
            }.get(state, "uncertain")
            await conn.execute("UPDATE media SET delivery_state=%s WHERE id=%s", (state, media_id))
            session_id = await handoffs.lock_for_reply(conn, target.chat_id, user["user_id"])
            assert session_id is not None
            await handoffs.touch(conn, session_id)
    except inbox.InboxError:
        state = "failed"
    except Exception as exc:
        logger.error("Attachment outcome uncertain (media_id=%s, reason=%s)", media_id, type(exc).__name__)
        # Do not resend after a transport or commit failure: the recipient may already have it.
        state = "uncertain"
    # A separate transaction persists a refused/uncertain result after rollback above.
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE media SET delivery_state=%s WHERE id=%s AND delivery_state<>%s",
            (state, media_id, "sent"),
        )
        row = await _existing(conn, user["user_id"], nonce)
    assert row
    return _result(row, False)
