"""Actual encrypted session/contact data through the Inbox query and API."""

# ruff: noqa: F811
import pytest

from core import content
from core.repositories import contacts
from core.repositories import inbox as repo
from tests.test_admin_room_pg import URL, client, database  # noqa: F401

pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


async def test_unnamed_whatsapp_contact_has_a_visible_number_in_list_and_conversation(
    client, database
):
    async with database.connection() as conn:
        person = await contacts.get_or_create(conn, "wa:15550001111")
        await contacts.record_message(
            conn, person.id, person.chat_id, "user", "Reação: 👍", "wa_event"
        )
        assert person.id is not None
        contact_id = person.id
    rows = (await client.get("/painel/api/inbox")).json()["conversations"]
    [row] = [r for r in rows if r["contact_id"] == contact_id]
    assert row["name"] is None and row["display_name"] == "+15550001111"
    assert "chat_id" not in row
    data = (await client.get(f"/painel/api/inbox/{contact_id}")).json()
    assert data["contact"]["display_name"] == "+15550001111"
    assert data["contact"]["phone"] == "+15550001111"
    assert data["messages"][0]["message"] == "Reação: 👍"
    async with database.connection() as conn:
        actual = await contacts.get_by_id(conn, contact_id)
        assert actual is not None and actual.name is None and actual.phone is None
        cur = await conn.execute(
            "SELECT message,message_sealed FROM sessions WHERE contact_id=%s", (contact_id,)
        )
        [(plain, sealed)] = await cur.fetchall()
        assert plain == "" and b"Rea" not in bytes(sealed)


async def test_old_placeholder_is_not_fabricated_as_an_attachment(database):
    async with database.connection() as conn:
        person = await contacts.get_or_create(conn, "wa:15550002222")
        await contacts.record_message(
            conn, person.id, person.chat_id, "user", content.text("inbox.unreadable"), "unreadable"
        )
        assert person.id is not None
        [row] = await repo.messages(conn, person.id)
        assert row["intent"] == "unreadable" and row["message"] == content.text("inbox.unreadable")
        cur = await conn.execute("SELECT count(*) FROM media WHERE contact_id=%s", (person.id,))
        assert await cur.fetchone() == (0,)
