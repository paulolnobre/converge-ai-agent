"""The morning briefing: what it says, and that it goes out once, in its window."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core import content
from core.config import settings
from core.handlers import message as handler
from core.integrations import telegram
from core.repositories import briefing as briefing_repo
from core.services import secretarial
from core.services.secretarial import Briefing

ZONE = ZoneInfo(settings.office_timezone)
TEAM = "-1001234567890"
THURSDAY_8H30 = datetime(2026, 9, 24, 8, 30, tzinfo=ZONE)


async def _value(value):
    return value


def test_a_full_briefing_names_each_thing_once():
    data = Briefing(
        today=[
            {"starts_at": THURSDAY_8H30.replace(hour=10, minute=0), "contact_name": "Ana",
             "confirmed": True},
            {"starts_at": THURSDAY_8H30.replace(hour=14, minute=0), "contact_name": None,
             "confirmed": False},
        ],
        new_contacts=[{"name": "Bruno", "stage": "respondido"}],
        inbound=7,
        handoffs=[{"contact_name": "Carla", "claimed_by_name": "Paulo"},
                  {"contact_name": "Davi", "claimed_by_name": None}],
        unrecorded=2,
        stale_proposals=[{"name": "Eva", "stage_changed_at": THURSDAY_8H30 - timedelta(days=5)}],
    )
    text = secretarial.compose(data, THURSDAY_8H30)

    assert text.startswith("Bom dia! Resumo de quinta-feira, 24/09.")
    assert "• 10:00 · Ana — confirmou" in text
    assert "• 14:00 · Sem nome — sem confirmação" in text
    assert content.render("briefing.new_contacts", total=1, mensagens=7) in text
    assert "• Bruno (Bot respondeu)" in text
    assert "• Carla — com Paulo" in text and "• Davi — ninguém assumiu" in text
    assert content.render("briefing.unrecorded", total=2) in text
    assert "• Eva (desde 19/09)" in text


def test_an_empty_day_still_says_so_and_leaves_out_empty_sections():
    text = secretarial.compose(Briefing(), THURSDAY_8H30)

    assert content.text("briefing.today_empty") in text
    assert content.render("briefing.no_new_contacts", mensagens=0) in text
    assert content.text("briefing.handoffs") not in text
    assert "comparecimento" not in text


def test_a_long_list_is_capped():
    data = Briefing(new_contacts=[{"name": f"P{i}", "stage": "novo"} for i in range(20)])
    text = secretarial.compose(data, THURSDAY_8H30)
    assert "• P14 (Novo lead)" in text and "P15" not in text
    assert "… e mais 5" in text


@pytest.mark.parametrize(("when", "expected"), [
    (THURSDAY_8H30, True),
    (THURSDAY_8H30.replace(hour=7), False),            # before the hour
    (THURSDAY_8H30.replace(hour=15), False),           # a late "Bom dia" is worse than none
    (THURSDAY_8H30 + timedelta(days=2), False),        # Saturday
])
def test_it_only_goes_out_in_the_window(monkeypatch: pytest.MonkeyPatch, when, expected):
    monkeypatch.setattr(settings, "daily_briefing_hour", 8)
    assert secretarial._in_window(when) is expected


@pytest.fixture
def team(monkeypatch: pytest.MonkeyPatch):
    posted: list[tuple[str, str]] = []

    async def post_message(chat_id, text, reply_markup=None, **kwargs):
        posted.append((str(chat_id), text))
        return 1

    monkeypatch.setattr(telegram, "post_message", post_message)
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    monkeypatch.setattr(settings, "daily_briefing_hour", 8)
    monkeypatch.setattr(secretarial, "gather", lambda conn, now, scheduled: _value(Briefing()))
    return posted


async def test_it_goes_out_once_a_day(monkeypatch: pytest.MonkeyPatch, team):
    claimed: set = set()

    async def claim_day(conn, day):
        if day in claimed:
            return False
        claimed.add(day)
        return True

    monkeypatch.setattr(briefing_repo, "claim_day", claim_day)

    assert await secretarial.send_if_due(object(), THURSDAY_8H30) is True
    assert await secretarial.send_if_due(object(), THURSDAY_8H30 + timedelta(minutes=5)) is False
    assert len(team) == 1 and team[0][0] == TEAM


async def test_without_a_team_group_nothing_is_claimed(monkeypatch: pytest.MonkeyPatch, team):
    monkeypatch.setattr(settings, "team_chat_id", "")
    monkeypatch.setattr(
        briefing_repo, "claim_day",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not claim")),
    )
    assert await secretarial.send_if_due(object(), THURSDAY_8H30) is False


def _group(chat_id: str, text: str) -> dict:
    return {"update_id": 9, "message": {
        "chat": {"id": int(chat_id), "type": "supergroup"}, "text": text,
        "from": {"id": 777, "first_name": "Paulo"},
    }}


async def test_resumo_in_the_team_group_posts_it_now(monkeypatch: pytest.MonkeyPatch, team):
    await handler.process_message(object(), _group(TEAM, "/resumo"))
    assert len(team) == 1 and "! Resumo de " in team[0][1]


async def test_resumo_anywhere_else_is_ignored(monkeypatch: pytest.MonkeyPatch, team):
    await handler.process_message(object(), _group("-100999", "/resumo@converge_bot"))
    assert team == []


def test_quiet_leads_are_listed_with_when_we_last_wrote():
    data = Briefing(followups=[
        {"id": 3, "name": "Fábio", "stage": "respondido",
         "last_at": THURSDAY_8H30 - timedelta(days=4)},
    ])
    text = secretarial.compose(data, THURSDAY_8H30)

    assert content.render("briefing.followups", dias=settings.followup_after_days) in text
    assert "• Fábio (Bot respondeu) — última mensagem nossa em 20/09" in text


async def test_a_stalled_proposal_is_not_named_twice(monkeypatch: pytest.MonkeyPatch):
    stalled = {"id": 5, "name": "Eva", "stage_changed_at": THURSDAY_8H30 - timedelta(days=6)}
    quiet = [
        {"id": 5, "name": "Eva", "stage": "proposta", "last_at": THURSDAY_8H30},
        {"id": 6, "name": "Gil", "stage": "respondido", "last_at": THURSDAY_8H30},
    ]
    for name, value in {
        "last_sent_before": None, "appointments_between": [], "new_contacts_since": [],
        "inbound_since": 0, "open_handoffs": [], "unrecorded_outcomes": 0,
        "stale_proposals": [stalled], "silent_leads": quiet,
    }.items():
        monkeypatch.setattr(briefing_repo, name, lambda *a, _v=value, **k: _value(_v))

    data = await secretarial.gather(object(), THURSDAY_8H30, scheduled=True)

    assert [item["name"] for item in data.stale_proposals] == ["Eva"]
    assert [item["name"] for item in data.followups] == ["Gil"]



@pytest.mark.parametrize(("hour", "greeting"), [(8, "Bom dia"), (14, "Boa tarde"), (19, "Boa noite")])
def test_the_greeting_follows_the_hour(hour, greeting):
    """/resumo at 18h used to open with "Bom dia!"."""
    text = secretarial.compose(Briefing(), THURSDAY_8H30.replace(hour=hour))
    assert text.startswith(f"{greeting}! Resumo de quinta-feira, 24/09.")


async def test_painel_in_the_team_group_gives_the_address(monkeypatch: pytest.MonkeyPatch, team):
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example.com")
    await handler.process_message(object(), _group(TEAM, "/painel"))
    assert len(team) == 1 and "https://bot.example.com/painel" in team[0][1]
