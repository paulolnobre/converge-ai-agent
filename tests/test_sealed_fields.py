"""The registry of encrypted fields: which ones, and how each is read and written."""
import logging
from pathlib import Path

import pytest

from core.crypto import seal
from core.sealed_fields import FIELDS, UNREADABLE, field, fingerprints, read, read_lenient, write

MIGRATION = Path(__file__).resolve().parent.parent / "infra/migrations/0009_sealed_fields.sql"


def test_the_registry_lists_exactly_the_spec_fields():
    assert {(f.table, f.column) for f in FIELDS} == {
        ("contacts", "name"), ("contacts", "email"), ("contacts", "phone"),
        ("contacts", "organization"), ("contacts", "notes"), ("contacts", "origin_detail"),
        ("contacts", "interest"), ("sessions", "message"), ("appointments", "title"),
        ("appointments", "meeting_url"), ("automation_queue", "reason"),
        ("mail_threads", "subject"), ("mail_drafts", "body_text"),
        ("media", "filename"),
        ("calendar_events", "title"), ("calendar_events", "attendees"),
        ("calendar_events", "meeting_url"),
        ("billing_customers", "tax_id"),
        ("proposal_briefings", "answers"),
        ("handoff_sessions", "reopen_reply"),
        ("proposal_quotes", "note"),
        ("contract_details", "data"),
    }
    assert {(f.table, f.column) for f in FIELDS if f.required} == {
        ("sessions", "message"), ("appointments", "title"), ("mail_drafts", "body_text"),
        ("proposal_briefings", "answers"), ("contract_details", "data"),
    }


def test_a_field_knows_its_context_and_sealed_column():
    f = field("contacts", "phone")
    assert f.context == "contacts.phone" and f.sealed == "phone_sealed"


def test_read_follows_the_state_table():
    message, notes = field("sessions", "message"), field("contacts", "notes")
    # Required: the sealed column wins once filled; the '' left behind is a sentinel.
    assert read(message, "", seal("sessions.message", "oi")) == "oi"
    # Required, not converted yet: the old column, even when it is ''.
    assert read(message, "", None) == ""
    assert read(message, "antigo", None) == "antigo"
    # Optional: the old column wins while it holds anything, '' included.
    assert read(notes, "", None) == ""
    assert read(notes, "antiga", seal("contacts.notes", "nova")) == "antiga"
    assert read(notes, None, seal("contacts.notes", "nova")) == "nova"
    assert read(notes, None, None) is None


def test_write_empties_the_old_column_by_kind():
    message = write(field("sessions", "message"), "oi")
    assert message["message"] == "" and isinstance(message["message_sealed"], bytes)
    notes = write(field("contacts", "notes"), None)
    assert notes == {"notes": None, "notes_sealed": None}


def test_read_lenient_marks_an_unreadable_value_and_logs_no_content(caplog):
    name = field("contacts", "name")
    box = seal("contacts.name", "Maria Silva")
    assert box is not None
    tampered = box[:-1] + bytes([box[-1] ^ 1])
    with caplog.at_level(logging.ERROR):
        assert read_lenient(name, None, tampered, row_id=7) == UNREADABLE
    assert "contacts.name" in caplog.text and "id=7" in caplog.text
    assert "Maria" not in caplog.text


def test_read_raises_on_an_unreadable_value():
    from core.crypto import SealedDataError

    with pytest.raises(SealedDataError):
        read(field("contacts", "name"), None, b"\x01\x01" + bytes(40))


def test_fingerprints_cover_phone_and_email():
    prints = fingerprints("(91) 98888-7777", "Ana@X.com")
    assert set(prints) == {"phone_fp", "email_fp"} and all(prints.values())
    assert fingerprints(None, None) == {"phone_fp": None, "email_fp": None}


def test_the_migration_has_a_sealed_column_for_every_field():
    sql = MIGRATION.read_text()
    for f in FIELDS:
        if f.table in ("media", "calendar_events", "billing_customers", "proposal_briefings",
                       "handoff_sessions", "proposal_quotes", "contract_details"):
            continue  # born sealed in later migrations (below)
        assert f"ADD COLUMN IF NOT EXISTS {f.sealed} BYTEA" in sql, f.context
    media_sql = (MIGRATION.parent / "0011_media.sql").read_text()
    assert "filename_sealed BYTEA" in media_sql
    assert "CONSTRAINT media_filename_sealed_only CHECK (filename IS NULL)" in media_sql
    calendar_sql = (MIGRATION.parent / "0012_calendar_events.sql").read_text()
    for column in ("title", "attendees", "meeting_url"):
        assert f"{column}_sealed" in calendar_sql
        assert (f"CONSTRAINT calendar_events_{column}_sealed_only CHECK ({column} IS NULL)"
                in calendar_sql)
    billing_sql = (MIGRATION.parent / "0019_billing.sql").read_text()
    assert "tax_id_sealed BYTEA" in billing_sql
    assert "CONSTRAINT billing_customers_tax_id_sealed_only CHECK (tax_id IS NULL)" in billing_sql
    proposal_sql = (MIGRATION.parent / "0023_proposal_briefings.sql").read_text()
    assert "answers_sealed BYTEA" in proposal_sql
    assert "CONSTRAINT proposal_briefings_answers_sealed_only CHECK (answers = '')" in proposal_sql
    reopen_sql = (MIGRATION.parent / "0025_reopen_reply.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS reopen_reply_sealed BYTEA" in reopen_sql
    assert ("CONSTRAINT handoff_sessions_reopen_reply_sealed_only CHECK (reopen_reply IS NULL)"
            in reopen_sql)
    quote_sql = (MIGRATION.parent / "0026_proposal_quotes.sql").read_text()
    assert "note_sealed            BYTEA" in quote_sql
    assert "CONSTRAINT proposal_quotes_note_sealed_only CHECK (note IS NULL)" in quote_sql
    contract_sql = (MIGRATION.parent / "0027_contract_details.sql").read_text()
    assert "data_sealed  BYTEA" in contract_sql
    assert "CONSTRAINT contract_details_data_sealed_only CHECK (data = '')" in contract_sql
    for column in ("phone_fp BYTEA", "email_fp BYTEA",
                   "test_mode BOOLEAN NOT NULL DEFAULT FALSE"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql
    assert "CREATE TABLE IF NOT EXISTS data_format" in sql
    assert "UPDATE " not in sql.upper().replace("ON UPDATE", "")


def test_pair_sql_names_both_columns_under_one_key():
    from core.sealed_fields import pair_sql

    assert pair_sql("c", field("contacts", "name"), "contact_name") == (
        "c.name AS contact_name, c.name_sealed AS contact_name_sealed")
    assert pair_sql("c", field("contacts", "name")) == "c.name AS name, c.name_sealed AS name_sealed"


def test_open_dict_reads_each_pair_and_drops_the_sealed_key(caplog):
    from core.sealed_fields import open_dict

    box = seal("contacts.name", "Maria")
    assert box is not None
    broken = box[:-1] + bytes([box[-1] ^ 1])
    rows = [{"id": 1, "contact_name": None, "contact_name_sealed": box},
            {"id": 2, "contact_name": "João", "contact_name_sealed": None},
            {"id": 3, "contact_name": None, "contact_name_sealed": broken}]
    pairs = [(field("contacts", "name"), "contact_name")]
    opened = [open_dict(row, pairs) for row in rows]
    assert [r["contact_name"] for r in opened] == ["Maria", "João", UNREADABLE]
    assert all("contact_name_sealed" not in r for r in opened)
    assert "Maria" not in caplog.text
