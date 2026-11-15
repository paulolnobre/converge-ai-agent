"""Raw bounded uploads. Authentication runs before reading any file bytes."""

from __future__ import annotations

import asyncio
import base64
import binascii
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from core import content
from core.config import settings
from core.repositories import admin_room, media
from core.security import RateLimiter
from core.services import (
    contract_sign,
    panel_auth,
    panel_uploads,
    proposal_info,
    proposal_pdf,
    proposal_send,
)
from core.services.upload_types import MAX_BYTES, InvalidUpload, Upload, clean_name, normalize
from core.web import panel_media

_slots = asyncio.Semaphore(2)
_limiter = RateLimiter(max_events=12, window_seconds=60)


class Metadata(BaseModel):
    nonce: UUID
    kind: Literal["image", "audio", "document"]
    filename: str = Field(min_length=1, max_length=120)
    caption: str = Field(default="", max_length=500)


async def receive(request: Request) -> tuple[Metadata, bytes]:
    header = request.headers.get("x-upload-metadata", "")
    if not header or len(header) > 4096:
        raise HTTPException(422, "Metadados do anexo inválidos.")
    try:
        meta = Metadata.model_validate_json(base64.b64decode(header, validate=True))
    except (ValueError, ValidationError, binascii.Error):
        raise HTTPException(422, "Metadados do anexo inválidos.") from None
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > MAX_BYTES):
        raise HTTPException(413, "O arquivo pode ter até 20 MB.")
    data = bytearray()
    try:
        async with asyncio.timeout(60):
            async for chunk in request.stream():
                if len(data) + len(chunk) > MAX_BYTES:
                    raise HTTPException(413, "O arquivo pode ter até 20 MB.")
                data.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, "O envio demorou demais. Tente de novo.") from None
    return meta, bytes(data)


def router(require_session, get_pool, private, session_cookie: str) -> APIRouter:
    routes = APIRouter(prefix="/api", dependencies=[Depends(require_session)])

    async def post(request, user, contact_id):
        if not settings.panel_uploads_enabled:
            raise HTTPException(503, "O envio de anexos ainda não está habilitado.")
        token_hash = panel_auth.digest(request.cookies.get(session_cookie, ""))
        pool = await get_pool()
        try:
            # Refuse access before upload, decode, encryption or a model can see bytes.
            async with pool.connection() as conn:
                await panel_uploads.authorize(conn, user, token_hash, contact_id)
            if not _limiter.check(user["user_id"])[0]:
                raise HTTPException(429, "Muitos anexos. Aguarde um minuto.")
            try:
                await asyncio.wait_for(_slots.acquire(), timeout=0.1)
            except TimeoutError:
                raise HTTPException(
                    503, "Há outros anexos em processamento. Tente em instantes."
                ) from None
            try:
                meta, data = await receive(request)
                digest = panel_uploads.fingerprint(data, meta.kind, meta.filename, meta.caption)
                upload = await normalize(data, meta.kind, meta.filename)
                result = await panel_uploads.deliver(
                    pool,
                    user,
                    token_hash,
                    contact_id,
                    meta.nonce,
                    digest,
                    upload,
                    meta.caption.strip(),
                )
                return private(result)
            finally:
                _slots.release()
        except panel_uploads.inbox.InboxError as exc:
            raise HTTPException(exc.status, exc.detail) from None
        except InvalidUpload as exc:
            raise HTTPException(422, str(exc)) from None
        except TimeoutError:
            raise HTTPException(
                422, "Não consegui preparar o áudio no prazo. Grave um trecho menor."
            ) from None

    @routes.post("/inbox/{contact_id}/attachments")
    async def inbox_attachment(
        contact_id: int, request: Request, user: dict = Depends(require_session)
    ):
        return await post(request, user, contact_id)

    @routes.post("/proposals/{briefing_id}/document")
    async def proposal_document(briefing_id: int, request: Request,
                                user: dict = Depends(require_session)):
        """The proposal PDF, to the person who asked for it (core/services/proposal_send.py)."""
        if not _limiter.check(user["user_id"])[0]:
            raise HTTPException(429, "Muitos anexos. Aguarde um minuto.")
        try:
            await asyncio.wait_for(_slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise HTTPException(503, "Há outros anexos em processamento. Tente em instantes.") from None
        try:
            meta, data = await receive(request)
            if meta.kind != "document":
                raise HTTPException(422, "Envie a proposta em PDF.")
            upload = await normalize(data, "document", meta.filename)
            token_hash = panel_auth.digest(request.cookies.get(session_cookie, ""))
            return private(await proposal_send.send(await get_pool(), user, token_hash,
                                                    briefing_id, upload))
        except panel_uploads.inbox.InboxError as exc:
            raise HTTPException(exc.status, exc.detail) from None
        except InvalidUpload as exc:
            raise HTTPException(422, str(exc)) from None
        except TimeoutError:
            raise HTTPException(504, "O envio demorou demais. Confira a conversa antes de tentar de novo.") from None
        finally:
            _slots.release()

    @routes.post("/proposals/{briefing_id}/document/generated")
    async def proposal_generated(briefing_id: int, request: Request,
                                 user: dict = Depends(require_session)):
        """The proposal made from the model (core/services/proposal_pdf.py), sent
        the same way as an uploaded PDF."""
        if not _limiter.check(user["user_id"])[0]:
            raise HTTPException(429, "Muitos anexos. Aguarde um minuto.")
        try:
            await asyncio.wait_for(_slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise HTTPException(503, "Há outros anexos em processamento. Tente em instantes.") from None
        try:
            pool = await get_pool()
            async with pool.connection() as conn:
                data, name = await proposal_pdf.generate(conn, briefing_id)
            upload = Upload(data=data, kind="document", mime="application/pdf",
                            filename=clean_name(name))
            token_hash = panel_auth.digest(request.cookies.get(session_cookie, ""))
            return private(await proposal_send.send(pool, user, token_hash, briefing_id,
                                                    upload))
        except proposal_pdf.NoQuote:
            raise HTTPException(409, content.text("proposal_pdf.sem_orcamento")) from None
        except LookupError:
            raise HTTPException(404, "Pedido não encontrado.") from None
        except panel_uploads.inbox.InboxError as exc:
            raise HTTPException(exc.status, exc.detail) from None
        except TimeoutError:
            raise HTTPException(504, "O envio demorou demais. Confira a conversa antes de tentar de novo.") from None
        finally:
            _slots.release()

    @routes.post("/proposals/{briefing_id}/info")
    async def proposal_info_request(briefing_id: int, request: Request,
                                    user: dict = Depends(require_session)):
        """Ask the person for more information to build the proposal (core/services/proposal_info.py)."""
        if not _limiter.check(user["user_id"])[0]:
            raise HTTPException(429, "Muitos envios. Aguarde um minuto.")
        try:
            token_hash = panel_auth.digest(request.cookies.get(session_cookie, ""))
            return private(await proposal_info.ask(await get_pool(), user, token_hash,
                                                    briefing_id))
        except panel_uploads.inbox.InboxError as exc:
            raise HTTPException(exc.status, exc.detail) from None

    @routes.post("/proposals/{briefing_id}/contract/sign")
    async def contract_sign_request(briefing_id: int, request: Request,
                                    user: dict = Depends(require_session)):
        """The contract to Autentique, to be signed (core/services/contract_sign.py)."""
        if not _limiter.check(user["user_id"])[0]:
            raise HTTPException(429, "Muitos envios. Aguarde um minuto.")
        try:
            token_hash = panel_auth.digest(request.cookies.get(session_cookie, ""))
            return private(await contract_sign.send(await get_pool(), user, token_hash,
                                                    briefing_id))
        except panel_uploads.inbox.InboxError as exc:
            raise HTTPException(exc.status, exc.detail) from None

    @routes.post("/proposals/{briefing_id}/contract/release")
    async def contract_sign_release(briefing_id: int, request: Request,
                                    user: dict = Depends(require_session)):
        """A send to Autentique never confirmed, freed to be sent again."""
        try:
            token_hash = panel_auth.digest(request.cookies.get(session_cookie, ""))
            return private(await contract_sign.release(await get_pool(), user, token_hash,
                                                       briefing_id))
        except panel_uploads.inbox.InboxError as exc:
            raise HTTPException(exc.status, exc.detail) from None

    @routes.post("/admin/attachments")
    async def admin_attachment(request: Request, user: dict = Depends(require_session)):
        return await post(request, user, None)

    @routes.api_route("/admin/media/{media_id}", methods=["GET", "HEAD"])
    async def admin_file(media_id: int, request: Request, user: dict = Depends(require_session)):
        pool = await get_pool()
        async with pool.connection() as conn:
            try:
                await admin_room.require_member(conn, user["user_id"])
            except admin_room.NotMember:
                raise HTTPException(403, "Você não participa do grupo administrativo.") from None
            found = await media.admin_servable(conn, media_id)
            if found is None:
                return panel_media._empty(404)
            return await panel_media.serve_record(
                conn,
                found,
                method=request.method,
                range_header=request.headers.get("range"),
                download=request.query_params.get("download") == "1",
            )

    return routes
