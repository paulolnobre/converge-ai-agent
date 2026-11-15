"""The contract signed on Autentique: its webhook (spec venda-ate-pagamento,
increment 4, part 3B).

The route checks the signature and queues only what this needs (never the
signers' CPF or e-mail): the event, the document and the signature. Applying
it is idempotent, as Autentique repeats and reorders events: a party signs
once, the document finishes once, and the team hears once. Then the first payment of the setup is
charged (contract_charge).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import datetime

from core.repositories import contacts as contacts_repo
from core.repositories import contract_signatures as repo
from core.repositories import proposal_briefings as briefings_repo
from core.services import contract_charge, proposal_briefing

logger = logging.getLogger(__name__)

SIGNED = "signature.accepted"
FINISHED = "document.finished"
_ID = re.compile(r"[A-Za-z0-9-]{1,100}")
_FILE = re.compile(r"https://[A-Za-z0-9.-]+\.autentique\.com\.br/[^\s]{1,500}")


def verify(body: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256 of the raw body, in hex, as Autentique signs it."""
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.strip().lower().encode(), expected.encode())


def _id(value: object) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _when(value: object) -> str | None:
    """Autentique's ISO time, kept as text in the queue; None when it is not one."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def slim(payload: object) -> dict | None:
    """What the queue keeps of one webhook call; None for an event this does
    not use (acknowledged, never queued). ValueError when it is not one."""
    event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, dict):
        raise ValueError("no event")
    event_id, kind = _id(event.get("id")), event.get("type")
    if event_id is None or not isinstance(kind, str):
        raise ValueError("no event id or type")
    if kind not in (SIGNED, FINISHED):
        return None
    data = event.get("data")
    data = data if isinstance(data, dict) else {}
    # The examples put the object straight in "data" or under "data.object".
    inner = data.get("object")
    obj: dict = inner if isinstance(inner, dict) else data
    if kind == SIGNED:
        document = obj.get("document")
        if isinstance(document, dict):
            document = document.get("id")
        found = {"document": _id(document), "signature": _id(obj.get("public_id")),
                 "at": _when(obj.get("signed"))}
    else:
        files = obj.get("files")
        signed_file = files.get("signed") if isinstance(files, dict) else None
        found = {"document": _id(obj.get("id")),
                 "file": signed_file if isinstance(signed_file, str)
                 and _FILE.fullmatch(signed_file) else None,
                 "at": _when(event.get("created_at"))}
    if found["document"] is None:
        raise ValueError("no document id")
    return {"id": event_id, "type": kind, **found}


async def must_wait(conn, document: str) -> bool:
    """A document not known yet while a contract is still being sent: its id
    may be about to be written, so Autentique is asked to try again later
    (it does, after 60, 120 and 300 seconds) rather than the event lost."""
    return await repo.by_document(conn, document) is None and await repo.sending_any(conn)


def _at(event: dict) -> datetime | None:
    value = _when(event.get("at"))
    return datetime.fromisoformat(value) if value else None


async def handle_event(conn, event: dict) -> str:
    """Apply one queued event; the team hears once the document is finished."""
    document = _id(event.get("document"))
    if document is None:
        return "ignored"
    sent = await repo.by_document(conn, document)
    if sent is None:
        # Another document of this account (signed in the dashboard, say).
        logger.info("Autentique event for a document this does not know")
        return "unknown"
    if event.get("type") == SIGNED:
        signature = event.get("signature")
        party = ("customer" if signature and signature == sent.customer_signature else
                 "converge" if signature and signature == sent.converge_signature else None)
        if party is None:
            logger.warning("Autentique signature matches neither party of request %s",
                           sent.briefing_id)
            return "unknown"
        changed = await repo.mark_party_signed(conn, sent.briefing_id, party, _at(event))
        await conn.commit()
        return f"signed:{party}" if changed else "duplicate"
    if event.get("type") != FINISHED:
        return "ignored"
    file = event.get("file")
    first = await repo.mark_signed(conn, sent.briefing_id,
                                   file if isinstance(file, str) else None, _at(event))
    await conn.commit()
    if first:
        await _tell_team(conn, sent)
    # Also when repeated: a charge left halfway (the worker died, say) is
    # picked up; one already made is never made again.
    charged = await contract_charge.charge_signed(conn, sent.briefing_id, sent.contact_id)
    return f"{'finished' if first else 'duplicate'}:{charged}"


async def _tell_team(conn, sent: repo.Document) -> None:
    """Best effort: the contract is already marked signed."""
    try:
        contact = await contacts_repo.get_by_id(conn, sent.contact_id)
        briefing = await briefings_repo.get(conn, sent.briefing_id)
        if contact is None or briefing is None:
            return
        await proposal_briefing.tell_team("contract_sign.team_signed", contact, briefing)
    except Exception as exc:
        logger.warning("Contract signed notice failed (%s)", type(exc).__name__)
