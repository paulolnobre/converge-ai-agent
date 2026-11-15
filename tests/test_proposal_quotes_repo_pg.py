"""Preliminary quotes against a real Postgres (TEST_DATABASE_URL): written
once per request, edited with who and when, the team's note sealed."""
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from core.config import settings
from core.repositories import proposal_briefings as briefings_repo
from core.repositories import proposal_quotes as repo
from core.services import pricing, proposal_quote

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
SHIPPED = Path(__file__).resolve().parent.parent / "content"


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    await connection.execute("SELECT 1")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


@pytest.fixture
def computed(monkeypatch: pytest.MonkeyPatch) -> Iterator[proposal_quote.Computed]:
    monkeypatch.setattr(settings, "content_dir", str(SHIPPED))
    pricing.reset()
    table = pricing.load_table()
    assert table is not None
    answers = pricing.Answers(canais=("WhatsApp",), sistemas=(), areas=("atendimento",),
                              volume_faixa="ate_500", roteiro_definido=True)
    scope = pricing.derive(answers, table)
    yield proposal_quote.Computed(answers=answers, scope=scope,
                                  quote=pricing.quote(scope, table), premissas=scope.premissas)
    pricing.reset()


async def _briefing(conn) -> int:
    cur = await conn.execute(
        "INSERT INTO contacts (chat_id, source) VALUES (%s, 'telegram') RETURNING id",
        (uuid.uuid4().hex[:12],))
    contact_id = (await cur.fetchone())[0]
    return (await briefings_repo.create(conn, contact_id, {}, [], None)).id


async def _user(conn) -> int:
    cur = await conn.execute(
        "INSERT INTO panel_users (email, name) VALUES (%s, 'Equipe') RETURNING id",
        (f"{uuid.uuid4().hex[:8]}@x.com",))
    return (await cur.fetchone())[0]


async def test_insert_once_keeps_the_first(conn, computed):
    briefing_id = await _briefing(conn)
    assert await repo.insert_once(conn, briefing_id, computed)
    user = await _user(conn)
    await repo.edit(conn, briefing_id, setup_cents=99900, monthly_cents=55500, note=None,
                    user_id=user)
    assert not await repo.insert_once(conn, briefing_id, computed)
    found = await repo.get(conn, briefing_id)
    assert found is not None
    assert (found.setup_cents, found.monthly_cents) == (99900, 55500)
    assert found.calc_setup_cents == computed.quote.setup_cents


async def test_a_new_quote_starts_at_the_calculated_values(conn, computed):
    briefing_id = await _briefing(conn)
    await repo.insert_once(conn, briefing_id, computed)
    found = await repo.get(conn, briefing_id)
    assert found is not None
    assert found.setup_cents == computed.quote.setup_cents
    assert found.monthly_cents == computed.quote.monthly_cents
    assert found.calc_setup_low_cents == computed.quote.setup_low_cents
    assert found.table_digest == computed.quote.table_digest
    assert found.scope["complexidade"] == computed.scope.complexidade
    assert found.inputs["volume_faixa"] == "ate_500"
    assert found.edited_by is None and found.edited_at is None


async def test_edit_records_who_and_when(conn, computed):
    briefing_id = await _briefing(conn)
    await repo.insert_once(conn, briefing_id, computed)
    user = await _user(conn)
    assert await repo.edit(conn, briefing_id, setup_cents=150050, monthly_cents=60000,
                           note="desconto de lançamento", user_id=user)
    found = await repo.get(conn, briefing_id)
    assert found is not None
    assert found.setup_cents == 150050 and found.edited_by == user
    assert found.edited_at is not None and found.note == "desconto de lançamento"


async def test_the_setup_terms_start_half_and_half_and_are_kept(conn, computed):
    briefing_id = await _briefing(conn)
    await repo.insert_once(conn, briefing_id, computed)
    user = await _user(conn)
    assert (await repo.get(conn, briefing_id)).forma_implantacao == "metade"  # type: ignore[union-attr]
    await repo.edit(conn, briefing_id, setup_cents=1_000_000, monthly_cents=60000, note=None,
                    user_id=user, forma="parcelado")
    assert (await repo.get(conn, briefing_id)).forma_implantacao == "parcelado"  # type: ignore[union-attr]
    await repo.edit(conn, briefing_id, setup_cents=1_000_000, monthly_cents=60000, note=None,
                    user_id=user)
    assert (await repo.get(conn, briefing_id)).forma_implantacao == "parcelado"  # type: ignore[union-attr]


async def test_unknown_setup_terms_are_refused_by_the_database(conn, computed):
    briefing_id = await _briefing(conn)
    await repo.insert_once(conn, briefing_id, computed)
    user = await _user(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        await repo.edit(conn, briefing_id, setup_cents=100, monthly_cents=1, note=None,
                        user_id=user, forma="entrada")


async def test_edit_without_a_quote_is_false(conn):
    briefing_id = await _briefing(conn)
    user = await _user(conn)
    assert not await repo.edit(conn, briefing_id, setup_cents=1, monthly_cents=1, note=None,
                               user_id=user)


async def test_the_note_is_sealed(conn, computed):
    briefing_id = await _briefing(conn)
    await repo.insert_once(conn, briefing_id, computed)
    user = await _user(conn)
    await repo.edit(conn, briefing_id, setup_cents=1, monthly_cents=1, note="segredo",
                    user_id=user)
    cur = await conn.execute(
        "SELECT note, note_sealed FROM proposal_quotes WHERE briefing_id = %s", (briefing_id,))
    note, sealed = await cur.fetchone()
    assert note is None and sealed is not None and b"segredo" not in bytes(sealed)


async def test_deleting_the_request_deletes_the_quote(conn, computed):
    briefing_id = await _briefing(conn)
    await repo.insert_once(conn, briefing_id, computed)
    await conn.execute("DELETE FROM proposal_briefings WHERE id = %s", (briefing_id,))
    assert await repo.get(conn, briefing_id) is None


async def test_a_failed_insert_leaves_the_update_usable(conn, computed):
    """One transaction per update: a quote that fails to write must not abort
    the request that is being sent to the team in the same transaction."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await repo.insert_once(conn, 10**12, computed)  # no such request
    briefing_id = await _briefing(conn)
    assert await repo.insert_once(conn, briefing_id, computed)


async def test_quotes_made_before_keep_their_installments(conn, computed):
    """Migration 0031 on a database with quotes: theirs stay "parcelado", as
    their proposals and contracts said; new ones start half and half."""
    migration = (Path(__file__).resolve().parent.parent
                 / "infra/migrations/0031_quote_setup_terms.sql").read_text()
    await conn.execute("ALTER TABLE proposal_quotes "
                       "DROP CONSTRAINT proposal_quotes_forma_implantacao, "
                       "DROP COLUMN forma_implantacao")
    old = await _briefing(conn)
    await repo.insert_once(conn, old, computed)
    for _ in range(2):  # safe to repeat
        await conn.execute(migration)
    new = await _briefing(conn)
    await repo.insert_once(conn, new, computed)
    assert (await repo.get(conn, old)).forma_implantacao == "parcelado"  # type: ignore[union-attr]
    assert (await repo.get(conn, new)).forma_implantacao == "metade"  # type: ignore[union-attr]
