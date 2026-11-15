"""Contracts sent to be signed, against a real Postgres (TEST_DATABASE_URL):
claimed once, released only while still "sending", and read back."""
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from core.repositories import contract_signatures as repo
from core.repositories import proposal_briefings as briefings_repo

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
MIGRATIONS = [Path(__file__).resolve().parent.parent / f"infra/migrations/{name}"
              for name in ("0029_contract_signatures.sql", "0030_contract_signed.sql")]


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    async with connection.cursor() as cur:
        for _ in range(2):  # safe to repeat
            for migration in MIGRATIONS:
                await cur.execute(migration.read_text())  # type: ignore[arg-type]
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _briefing(conn) -> int:
    cur = await conn.execute(
        "INSERT INTO contacts (chat_id, source) VALUES (%s, 'telegram') RETURNING id",
        (uuid.uuid4().hex[:12],))
    contact_id = (await cur.fetchone())[0]
    return (await briefings_repo.create(conn, contact_id, {}, [], None)).id


async def _user(conn) -> int:
    cur = await conn.execute("INSERT INTO panel_users (email, name, password_hash) "
                             "VALUES (%s, 'P', 'h') RETURNING id", (f"{uuid.uuid4().hex}@x.com",))
    return (await cur.fetchone())[0]


async def test_claimed_once_then_sent(conn):
    b, user = await _briefing(conn), await _user(conn)

    attempt = await repo.claim(conn, b, user, sandbox=True)
    assert attempt is not None
    assert await repo.claim(conn, b, user, sandbox=False) is None
    sending = await repo.get(conn, b)
    assert sending is not None and sending.status == "sending" and sending.sandbox

    assert await repo.mark_sent(conn, attempt, "doc1", "sig-ana", "sig-paulo")
    await repo.release(conn, attempt)  # too late: it went

    sent = await repo.get(conn, b)
    assert sent is not None
    assert (sent.status, sent.document_id, sent.customer_signature, sent.sent_by) == (
        "sent", "doc1", "sig-ana", user)
    assert sent.sent_at is not None


async def test_released_while_sending_can_be_claimed_again(conn):
    b, user = await _briefing(conn), await _user(conn)
    attempt = await repo.claim(conn, b, user, sandbox=False)
    assert attempt is not None

    await repo.release(conn, attempt)

    assert await repo.get(conn, b) is None
    assert await repo.claim(conn, b, user, sandbox=False) is not None


async def test_one_document_per_request(conn):
    first, second, user = await _briefing(conn), await _briefing(conn), await _user(conn)
    one = await repo.claim(conn, first, user, sandbox=False)
    two = await repo.claim(conn, second, user, sandbox=False)
    assert one is not None and two is not None
    await repo.mark_sent(conn, one, "doc1", None, None)

    with pytest.raises(psycopg.errors.UniqueViolation):
        await repo.mark_sent(conn, two, "doc1", None, None)


async def test_only_a_stale_send_is_freed(conn):
    b, user = await _briefing(conn), await _user(conn)
    await repo.claim(conn, b, user, sandbox=False)

    assert not await repo.release_stale(conn, b, 2), "still under way"
    await conn.execute("UPDATE contract_signatures SET created_at = NOW() - INTERVAL '3 minutes' "
                       "WHERE briefing_id = %s", (b,))
    assert await repo.release_stale(conn, b, 2)
    assert await repo.get(conn, b) is None


async def test_a_sent_contract_is_never_freed(conn):
    b, user = await _briefing(conn), await _user(conn)
    attempt = await repo.claim(conn, b, user, sandbox=False)
    assert attempt is not None
    await repo.mark_sent(conn, attempt, "doc1", None, None)
    await conn.execute("UPDATE contract_signatures SET created_at = NOW() - INTERVAL '1 hour' "
                       "WHERE briefing_id = %s", (b,))

    assert not await repo.release_stale(conn, b, 2)


async def test_an_old_attempt_never_writes_over_a_new_one(conn):
    b, user = await _briefing(conn), await _user(conn)
    old = await repo.claim(conn, b, user, sandbox=False)
    assert old is not None
    await conn.execute("UPDATE contract_signatures SET created_at = NOW() - INTERVAL '3 minutes' "
                       "WHERE briefing_id = %s", (b,))
    assert await repo.release_stale(conn, b, 2)
    new = await repo.claim(conn, b, user, sandbox=False)
    assert new is not None

    assert not await repo.mark_sent(conn, old, "doc-old", None, None)
    assert await repo.mark_sent(conn, new, "doc-new", None, None)
    sent = await repo.get(conn, b)
    assert sent is not None and sent.document_id == "doc-new"


async def test_a_claim_is_dated_when_made_not_when_its_transaction_began(conn):
    b, user = await _briefing(conn), await _user(conn)
    await conn.execute("SELECT pg_sleep(0.05)")

    await repo.claim(conn, b, user, sandbox=False)

    cur = await conn.execute("SELECT created_at > NOW() FROM contract_signatures "
                             "WHERE briefing_id = %s", (b,))
    assert (await cur.fetchone())[0]


async def test_found_by_document_then_signed_once(conn):
    b, user = await _briefing(conn), await _user(conn)
    attempt = await repo.claim(conn, b, user, sandbox=True)
    assert attempt is not None
    assert await repo.by_document(conn, "doc-x") is None
    await repo.mark_sent(conn, attempt, "doc-x", "sig-ana", "sig-paulo")

    doc = await repo.by_document(conn, "doc-x")
    assert doc is not None and (doc.briefing_id, doc.status, doc.customer_signature,
                                doc.converge_signature) == (b, "sent", "sig-ana", "sig-paulo")

    ana = datetime(2025, 1, 31, 12, 22, 30, tzinfo=UTC)
    assert await repo.mark_party_signed(conn, b, "customer", ana)
    assert not await repo.mark_party_signed(conn, b, "customer", None)
    half = await repo.get(conn, b)
    assert half is not None and half.customer_signed_at == ana
    assert half.converge_signed_at is None

    finished = datetime(2025, 1, 31, 13, 0, tzinfo=UTC)
    assert await repo.mark_signed(conn, b, "https://painel.autentique.com.br/d/a.pdf", finished)
    assert not await repo.mark_signed(conn, b, None, None)
    done = await repo.get(conn, b)
    assert done is not None and done.status == "signed" and done.signed_at == finished
    assert done.signed_file == "https://painel.autentique.com.br/d/a.pdf"
    # The Converge's signature, delivered after the finish, still gets its time.
    assert await repo.mark_party_signed(conn, b, "converge", None)
    late = await repo.get(conn, b)
    assert late is not None and late.converge_signed_at is not None


async def test_a_contract_still_sending_is_never_signed(conn):
    b, user = await _briefing(conn), await _user(conn)
    await repo.claim(conn, b, user, sandbox=True)
    assert await repo.sending_any(conn)
    assert not await repo.mark_party_signed(conn, b, "customer", None)
    assert not await repo.mark_signed(conn, b, None, None)


async def test_the_contract_keeps_the_setup_payments_it_stated(conn):
    from core.services.pricing import SetupPlan
    b, user = await _briefing(conn), await _user(conn)
    terms = SetupPlan("parcelado", 6, 166_670, 166_666)
    await repo.claim(conn, b, user, sandbox=True, terms=terms)
    sent = await repo.get(conn, b)
    assert sent is not None and sent.terms == terms


async def test_a_contract_sent_before_has_no_terms(conn):
    b, user = await _briefing(conn), await _user(conn)
    await repo.claim(conn, b, user, sandbox=True)
    sent = await repo.get(conn, b)
    assert sent is not None and sent.terms is None
