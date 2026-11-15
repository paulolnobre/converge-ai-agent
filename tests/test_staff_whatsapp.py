"""The team on WhatsApp: a staff member's number is recognized, so their own
chats never show up as customers — in either form Meta may send it."""
from datetime import datetime

import pytest

from core import channels
from core.repositories import automations as automations_repo
from core.repositories import briefing as briefing_repo
from core.repositories import contacts as contacts_repo
from core.repositories import funnel as funnel_repo
from core.repositories import inbox as inbox_repo

# ── The number, in every form WhatsApp may send it ───────────────────────────

@pytest.mark.parametrize("typed", [
    "+55 91 8000-0002", "5591 8000 0002", "55 (91) 8000-0002", "559180000002",
])
def test_an_old_format_mobile_number_is_known_with_and_without_the_ninth_digit(typed):
    assert set(channels.whatsapp_ids(typed)) == {"wa:559180000002", "wa:5591980000002"}


def test_a_new_format_mobile_number_is_known_both_ways_too():
    assert set(channels.whatsapp_ids("+55 91 98000-0002")) == {
        "wa:5591980000002", "wa:559180000002",
    }


def test_a_landline_does_not_gain_a_ninth_digit():
    assert channels.whatsapp_ids("+55 91 3222-4567") == ["wa:559132224567"]


def test_a_number_from_elsewhere_is_taken_as_it_is():
    assert channels.whatsapp_ids("+1 415 555 0100") == ["wa:14155550100"]


@pytest.mark.parametrize("nothing", [None, "", "   ", "sem número"])
def test_no_number_means_no_ids(nothing):
    assert channels.whatsapp_ids(nothing) == []


def test_dropping_the_nine_never_turns_a_mobile_into_someone_elses_landline():
    """9 3222-4567 is a mobile; 3222-4567 is a landline, maybe a customer's."""
    ids = channels.whatsapp_ids("+55 91 93222-4567")

    assert ids == ["wa:5591932224567"]
    assert not set(ids) & set(channels.whatsapp_ids("+55 91 3222-4567"))


@pytest.mark.parametrize("unusable", [
    "(91) 98000-0002",        # no country code: 91 would be read as one
    "+55 91 9XXXX-XXXX",      # the example file's placeholder
    "+55 91 8182",            # too short
    "4155550100",             # foreign without + : no way to know the country
])
def test_a_number_that_cannot_be_a_whatsapp_id_gives_none(unusable):
    assert channels.whatsapp_ids(unusable) == []


async def test_loading_says_when_a_whatsapp_number_was_not_stored(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    from cli import load_content
    from core import content

    async def nothing(*args, **kwargs):
        return 1

    monkeypatch.setattr(content, "staff", lambda: [
        {"name": "Victor", "email": "v@x.com", "whatsapp": "(91) 8887-7174"},
        {"name": "Paulo", "email": "p@x.com", "whatsapp": "+55 91 8000-0002"},
    ])
    monkeypatch.setattr(contacts_repo, "upsert_staff", nothing)
    monkeypatch.setattr(contacts_repo, "set_escalation", nothing)

    await load_content._load_staff(None)

    out = capsys.readouterr().out
    assert "Victor" in out and "(91) 8887-7174" in out
    assert "Paulo" not in out.replace("Staff: 2 people", "")


# ── Every place that leaves the team out of the customer lists ───────────────

class _Cursor:
    def __init__(self, log: list[tuple], row: tuple | None):
        self.log = log
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.log.append((sql, params))

    async def fetchall(self):
        return []

    async def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row: tuple | None = None):
        self.calls: list[tuple] = []
        self.row = row

    def cursor(self, **kwargs):
        return _Cursor(self.calls, self.row)

    @property
    def sql(self) -> str:
        return " ".join(sql for sql, _ in self.calls)


async def _recorded(call, row: tuple | None = None) -> _Conn:
    conn = _Conn(row)
    await call(conn)
    return conn


@pytest.mark.parametrize("name, call", [
    ("inbox list", lambda c: inbox_repo.list_conversations(c, user_id=1)),
    ("inbox waiting count", lambda c: inbox_repo.count_waiting(c)),
    ("new-conversation notice", lambda c: inbox_repo.claim_conversation_notice(c, 1)),
    ("briefing: new contacts", lambda c: briefing_repo.new_contacts_since(c, datetime.now())),
    ("briefing: inbound", lambda c: briefing_repo.inbound_since(c, datetime.now())),
    ("briefing: silent leads", lambda c: briefing_repo.silent_leads(c, 3)),
    ("funnel and contacts list", lambda c: funnel_repo.list_leads(c)),
])
async def test_the_customer_lists_leave_out_whatsapp_staff_too(name, call):
    conn = await _recorded(call)

    assert "whatsapp_ids" in conn.sql, name


def test_automations_never_pick_whatsapp_staff():
    assert "whatsapp_ids" in automations_repo._CLIENT_OK


# ── Looking someone up, and loading the staff file ──────────────────────────

async def test_finding_staff_by_chat_matches_a_whatsapp_number():
    conn = await _recorded(lambda c: contacts_repo.find_staff_by_chat(c, "wa:559180000002"))

    assert "whatsapp_ids" in conn.sql
    assert conn.calls[0][1] == ("wa:559180000002", "wa:559180000002")


async def test_loading_the_staff_file_stores_both_forms_of_the_number():
    member = {"name": "Paulo", "email": "p@x.com", "chat_id": "1", "whatsapp": "+55 91 8000-0002"}

    conn = await _recorded(lambda c: contacts_repo.upsert_staff(c, member), row=(1,))

    assert "whatsapp_ids" in conn.sql
    stored = [p for p in conn.calls[0][1] if isinstance(p, list) and p and p[0].startswith("wa:")]
    assert len(stored) == 1 and set(stored[0]) == {"wa:559180000002", "wa:5591980000002"}
