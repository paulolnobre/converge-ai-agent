"""The customer fields the app keeps encrypted, and how each is read and written.

One list for the repositories, the conversion (cli.seal_data) and its check, so
a field cannot be sealed in one place and forgotten in another.

Every field has two columns side by side: the old one, in clear, and
``<column>_sealed`` (BYTEA). A row converted by this version keeps the old
column empty, and which "empty" depends on the column:

- required (NOT NULL): the old column holds '' and the sealed one the value;
  until converted, the sealed one is NULL and the old one holds the text,
  which may itself be '';
- optional: the old column is NULL once converted; until then it holds the
  text, '' included, and wins.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from core.crypto import SealedDataError, fingerprint, open_sealed, seal

logger = logging.getLogger(__name__)

UNREADABLE = "[ilegível]"


@dataclass(frozen=True)
class Field:
    table: str
    column: str
    required: bool = False

    @property
    def context(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def sealed(self) -> str:
        return f"{self.column}_sealed"


FIELDS: tuple[Field, ...] = (
    Field("contacts", "name"),
    Field("contacts", "email"),
    Field("contacts", "phone"),
    Field("contacts", "organization"),
    Field("contacts", "notes"),
    Field("contacts", "origin_detail"),
    Field("contacts", "interest"),
    Field("sessions", "message", required=True),
    Field("appointments", "title", required=True),
    Field("appointments", "meeting_url"),
    Field("automation_queue", "reason"),
    Field("mail_threads", "subject"),
    Field("mail_drafts", "body_text", required=True),
    Field("media", "filename"),
    Field("calendar_events", "title"),
    Field("calendar_events", "attendees"),
    Field("calendar_events", "meeting_url"),
    Field("billing_customers", "tax_id"),
    Field("proposal_briefings", "answers", required=True),
    Field("handoff_sessions", "reopen_reply"),
    Field("proposal_quotes", "note"),
    Field("contract_details", "data", required=True),
)

_BY_NAME = {(f.table, f.column): f for f in FIELDS}


def field(table: str, column: str) -> Field:
    return _BY_NAME[(table, column)]


def select_sql(alias: str, f: Field) -> str:
    """The two columns of a field, side by side, for a mapper to `read`."""
    prefix = f"{alias}." if alias else ""
    return f"{prefix}{f.column}, {prefix}{f.sealed}"


def read(f: Field, legacy: str | None, sealed: bytes | None,
         row_id: object = None) -> str | None:
    """The field's value by the state table. An envelope that does not open
    raises SealedDataError naming the field and the row — never the content."""
    if f.required and sealed is None:
        return legacy
    if not f.required and legacy is not None:
        return legacy
    try:
        return open_sealed(f.context, sealed)
    except SealedDataError:
        raise SealedDataError(f"{f.context} id={row_id} não abre") from None


def read_lenient(f: Field, legacy: str | None, sealed: bytes | None, *,
                 row_id: object) -> str | None:
    """`read`, for a list: one unreadable row shows UNREADABLE instead of
    taking the whole screen down. The log names the field and row, never the
    content."""
    try:
        return read(f, legacy, sealed)
    except SealedDataError:
        logger.error("Campo ilegível: %s id=%s", f.context, row_id)
        return UNREADABLE


def write(f: Field, value: str | None) -> dict[str, object]:
    """The two columns to write: the old one emptied, the sealed one filled."""
    return {f.column: "" if f.required else None, f.sealed: seal(f.context, value)}


def fingerprints(phone: str | None, email: str | None) -> dict[str, bytes | None]:
    return {"phone_fp": fingerprint("phone", phone), "email_fp": fingerprint("email", email)}


def pair_sql(alias: str, f: Field, key: str | None = None) -> str:
    """Both columns of a field under one output key, for dict rows:
    ``c.name AS contact_name, c.name_sealed AS contact_name_sealed``."""
    key = key or f.column
    return f"{alias}.{f.column} AS {key}, {alias}.{f.sealed} AS {key}_sealed"


def open_dict(row: dict, pairs, *, id_key: str = "id", lenient: bool = True) -> dict:
    """Replace each (field, key) pair of a dict row by its value, dropping the
    ``<key>_sealed`` entry. Lenient by default: these rows feed lists."""
    for f, key in pairs:
        sealed = row.pop(f"{key}_sealed", None)
        if lenient:
            row[key] = read_lenient(f, row.get(key), sealed, row_id=row.get(id_key))
        else:
            row[key] = read(f, row.get(key), sealed)
    return row
