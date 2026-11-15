"""The panel's agenda: each appointment carries its meeting link, so opening
one shows where the conversation happens."""
from datetime import datetime, timedelta

from core.repositories import funnel as funnel_repo


class _Cursor:
    def __init__(self, log: list[str]):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.log.append(sql)

    async def fetchall(self):
        return []


class _Conn:
    def __init__(self):
        self.sql: list[str] = []

    def cursor(self, **kwargs):
        return _Cursor(self.sql)


async def test_the_agenda_query_asks_for_the_meeting_link():
    conn = _Conn()
    now = datetime.now()

    await funnel_repo.appointments_between(conn, now, now + timedelta(days=1))

    assert "a.meeting_url" in conn.sql[0]


async def test_the_agenda_query_carries_the_sync_problem():
    """An appointment the calendar sync could not follow shows "precisa de atenção"."""
    conn = _Conn()
    now = datetime.now()

    await funnel_repo.appointments_between(conn, now, now + timedelta(days=1))

    assert "a.sync_problem" in conn.sql[0]
