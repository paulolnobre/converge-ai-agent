"""The contract's data against a real Postgres (TEST_DATABASE_URL): one sealed
document per request, written again when sent again, never in clear."""
import os
import uuid
from pathlib import Path

import psycopg
import pytest

from core.repositories import contract_details as repo
from core.repositories import proposal_briefings as briefings_repo

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
MIGRATION = Path(__file__).resolve().parent.parent / "infra/migrations/0027_contract_details.sql"


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    async with connection.cursor() as cur:
        await cur.execute(MIGRATION.read_text())  # type: ignore[arg-type]
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _contact(conn) -> int:
    cur = await conn.execute(
        "INSERT INTO contacts (chat_id, source) VALUES (%s, 'telegram') RETURNING id",
        (uuid.uuid4().hex[:12],))
    return (await cur.fetchone())[0]


async def _briefing(conn, contact_id: int | None = None) -> int:
    contact_id = contact_id or await _contact(conn)
    return (await briefings_repo.create(conn, contact_id, {}, [], None)).id


async def test_an_earlier_contract_of_the_same_contact_is_found(conn):
    ana, other = await _contact(conn), await _contact(conn)
    first = await _briefing(conn, ana)
    await repo.save(conn, first, {"cidade": "Belém"})
    # One open request per contact: the earlier one was signed and closed.
    await conn.execute("UPDATE proposal_briefings SET status = 'proposal_sent' WHERE id = %s",
                       (first,))
    await repo.save(conn, await _briefing(conn, other), {"cidade": "Manaus"})
    now = await _briefing(conn, ana)

    assert await repo.latest_of_contact(conn, ana, now) == {"cidade": "Belém"}
    assert await repo.latest_of_contact(conn, ana, first) is None, "not the same request"
    assert await repo.latest_of_contact(conn, other, now) == {"cidade": "Manaus"}


async def test_kept_sealed_and_read_back(conn):
    briefing_id = await _briefing(conn)
    assert await repo.get(conn, briefing_id) is None

    await repo.save(conn, briefing_id, {"documento": "11222333000181", "cidade": "Belém"})

    assert await repo.get(conn, briefing_id) == {"documento": "11222333000181",
                                                 "cidade": "Belém"}
    cur = await conn.execute("SELECT data, data_sealed IS NOT NULL FROM contract_details "
                             "WHERE briefing_id = %s", (briefing_id,))
    assert await cur.fetchone() == ("", True), "never in clear"


async def test_sent_again_replaces_it(conn):
    briefing_id = await _briefing(conn)
    await repo.save(conn, briefing_id, {"cidade": "Belém"})
    await repo.save(conn, briefing_id, {"cidade": "Ananindeua"})

    assert await repo.get(conn, briefing_id) == {"cidade": "Ananindeua"}


async def test_the_clear_column_refuses_text(conn):
    briefing_id = await _briefing(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        await conn.execute("INSERT INTO contract_details (briefing_id, data) VALUES (%s, 'x')",
                           (briefing_id,))


async def test_the_migration_applies_twice(conn):
    async with conn.cursor() as cur:
        await cur.execute(MIGRATION.read_text())  # type: ignore[arg-type]
