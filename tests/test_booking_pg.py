"""The booking flow's SQL against a real Postgres. Skipped unless
TEST_DATABASE_URL points at a throwaway database with infra/init.sql and every
migration applied; each test rolls back."""
import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from core.models import Appointment, Offer, TimeSlot
from core.repositories import appointments as appointments_repo
from core.repositories import contacts as contacts_repo
from core.repositories import offers as offers_repo

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
START = datetime(2030, 3, 5, 13, 0, tzinfo=UTC)


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    await connection.execute("SELECT 1")  # the outer transaction the rollback undoes
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


def _slot(minutes=0):
    start = START + timedelta(minutes=minutes)
    return TimeSlot(start=start, end=start + timedelta(minutes=30))


async def test_an_offer_keeps_its_period(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591911112222", name="Ana")
    await offers_repo.save(conn, Offer(contact_id=contact.id, chat_id="wa:5591911112222",
                                       slots=[_slot()], calendar_id="cal", period="morning"), 60)
    pending = await offers_repo.pending_for_chat(conn, "wa:5591911112222")
    assert pending is not None and pending.period == "morning"


async def test_a_booking_records_its_channel(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591911113333", name="Ana")
    saved = await appointments_repo.record(conn, Appointment(
        contact_id=contact.id, calendar_id="cal", provider="google", provider_event_id="ev-ch",
        title="Conversa", starts_at=START, ends_at=START + timedelta(minutes=30),
        booked_via="whatsapp"))
    assert saved is not None
    back = await appointments_repo.get_by_id(conn, saved.id or 0)
    assert back is not None and back.booked_via == "whatsapp"


async def test_two_bookings_at_once_do_not_pass_the_limit():
    """Review Focus 3: two connections, the same contact, limit 1. Each runs the
    locked part of take_offer (lock, count, write); only one writes."""
    import asyncio

    setup = await psycopg.AsyncConnection.connect(URL or "", autocommit=True)
    contact = await contacts_repo.get_or_create(setup, "wa:5591911114444", name="Ana")
    contact_id = contact.id or 0
    order: list[str] = []

    async def book(tag: str, minutes: int) -> bool:
        conn = await psycopg.AsyncConnection.connect(URL or "")
        try:
            await appointments_repo.lock_contact(conn, contact_id)
            order.append(f"{tag} locked")
            if await appointments_repo.count_upcoming(conn, contact_id, excluding=None) >= 1:
                await conn.rollback()
                return False
            await asyncio.sleep(0.2)  # the calendar round-trip, held under the lock
            await appointments_repo.record(conn, Appointment(
                contact_id=contact_id, calendar_id="cal", provider="google",
                provider_event_id=f"ev-race-{tag}", title="x",
                starts_at=datetime.now(UTC) + timedelta(days=3, minutes=minutes),
                ends_at=datetime.now(UTC) + timedelta(days=3, minutes=minutes + 30)))
            await conn.commit()
            return True
        finally:
            await conn.close()

    try:
        results = await asyncio.gather(book("a", 0), book("b", 60))
        assert sorted(results) == [False, True]
    finally:
        await setup.execute("DELETE FROM appointments WHERE contact_id = %s", (contact_id,))
        await setup.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
        await setup.close()


async def test_a_pending_booking_holds_every_named_day(conn):
    """Codex r2: the days named before the contact details are all kept."""
    from datetime import date

    from core.handlers.message import _booking_action
    from core.models import Triage

    days = [date(2030, 3, d) for d in range(1, 21)]
    action = _booking_action(Triage(intent="booking", preferred_period="afternoon"), days)
    contact = await contacts_repo.get_or_create(conn, "wa:5591911115555", name="Ana")
    await contacts_repo.set_pending_action(conn, contact.id or 0, action)
    back = await contacts_repo.get_by_id(conn, contact.id or 0)
    assert back is not None and back.pending_action == action and action.endswith("2030-03-20")
