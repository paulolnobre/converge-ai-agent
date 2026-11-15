"""Booking through the WhatsApp Flow: the free times go inside the Flow, and
its answer books through the same path as a tap on a time.

Driven through process_callback with the Desk of tests/test_desk_flow.py and
the fixed availability of tests/test_choose_day.py."""
# The fixtures are imported and taken as parameters: that is how pytest shares
# them, and ruff reads each use as a redefinition.
# ruff: noqa: F811
import pytest

from core import content
from core.handlers import message as handler
from core.services import booking_lists as lists
from tests.test_choose_day import Availability, _at, _next
from tests.test_desk_flow import CONN, buttons, desk, reminded, tap  # noqa: F401
from tests.test_proposal_flow import wa_sends  # noqa: F401


def _flows(monkeypatch: pytest.MonkeyPatch, booking_id: str = "F-AGE") -> None:
    import shutil
    import tempfile
    from pathlib import Path

    from core.services import forms

    folder = Path(tempfile.mkdtemp()) / "forms"
    shutil.copytree(Path(__file__).resolve().parent.parent / "content" / "forms", folder)
    (folder / "flows.toml").write_text(f'[agendamento]\nflow_id = "{booking_id}"\n',
                                       encoding="utf-8")
    monkeypatch.setattr(forms, "_dir", lambda: folder)
    forms.reset()


@pytest.fixture
def times(monkeypatch: pytest.MonkeyPatch, desk):
    from core.services import scheduling

    available = Availability()
    available.starts = [_at(_next(4), 9), _at(_next(4), 15), _at(_next(5), 10)]
    available.starts.sort(key=lambda s: s.start)
    monkeypatch.setattr(scheduling, "available_starts", available)
    return available


def _answer(offer_id: int, **answers) -> dict:
    """The Flow's answer as the WhatsApp converter hands it on: a tap on
    `flow:<token>` carrying the answers."""
    update = tap(f"flow:agendar:{offer_id}", "cbflow")
    update["callback_query"]["_flow"] = {"nome": "Ana", "email": "ana@novo.com.br",
                                         "assunto": "", **answers}
    return update


def _flow(wa_sends) -> dict:
    return wa_sends["sent"][-1][1]["_flow"]


async def test_on_whatsapp_the_free_times_go_inside_the_flow(
        monkeypatch: pytest.MonkeyPatch, desk, wa_sends, times):
    _flows(monkeypatch)

    await handler.process_callback(CONN, tap("book:start"))

    text, _ = wa_sends["sent"][-1]
    flow = _flow(wa_sends)
    assert text == content.text("booking.flow_intro")
    assert flow["flow_id"] == "F-AGE" and flow["screen"] == "AGENDAR"
    assert flow["token"] == f"agendar:{desk.offer_calls}"
    titles = [o["title"] for o in flow["data"]["horarios"]]
    assert [o["id"] for o in flow["data"]["horarios"]] == ["0", "1", "2"]
    # The earliest first: on a Thursday, tomorrow's Friday comes before next Thursday.
    first = min(_next(4), _next(5))
    assert titles[0] == f"{lists.day_title(first)} às {lists.time_title(times.starts[0])}"
    assert all(len(t) <= 30 for t in titles)
    assert flow["data"]["email"] == "ana@example.com"
    assert desk.offers["42"].slots == times.starts


async def test_the_answer_books_the_time_picked(
        monkeypatch: pytest.MonkeyPatch, desk, wa_sends, times):
    _flows(monkeypatch)
    await handler.process_callback(CONN, tap("book:start"))
    offer_id = desk.offer_calls

    await handler.process_callback(CONN, _answer(offer_id, horario="1", assunto="proposta"))

    assert len(desk.created_events) == 1
    assert desk.created_events[0].start == times.starts[1].start
    assert "proposta" in desk.created_events[0].title
    assert desk.details.get("email") == "ana@novo.com.br"
    assert wa_sends["sent"][-1][1] is None or "_flow" not in (wa_sends["sent"][-1][1] or {})


@pytest.mark.parametrize("token_offset, horario",
                         [(1, "0"), (0, "9"), (0, "x"), (0, None), (0, "0" * 5000 + "1")])
async def test_an_answer_for_another_offer_or_time_books_nothing(
        monkeypatch: pytest.MonkeyPatch, desk, wa_sends, times, token_offset, horario):
    _flows(monkeypatch)
    await handler.process_callback(CONN, tap("book:start"))

    await handler.process_callback(
        CONN, _answer(desk.offer_calls + token_offset, horario=horario))

    assert desk.created_events == []
    assert content.text("scheduling.slot_taken") in wa_sends["sent"][-1][0]


async def test_a_refused_flow_falls_back_to_the_list_of_the_same_offer(
        monkeypatch: pytest.MonkeyPatch, desk, wa_sends, times):
    _flows(monkeypatch)
    wa_sends["refuse_flow"] = True

    await handler.process_callback(CONN, tap("book:start"))

    text, keyboard = wa_sends["sent"][-1]
    offer_id = desk.offer_calls
    assert buttons(keyboard) == [f"slot:{offer_id}:{i}" for i in range(3)]
    await handler.process_callback(CONN, tap(f"slot:{offer_id}:2", "cb2"))
    assert desk.created_events[0].start == times.starts[2].start


async def test_no_free_time_sends_no_flow(monkeypatch: pytest.MonkeyPatch, desk, wa_sends, times):
    _flows(monkeypatch)
    times.starts = []

    await handler.process_callback(CONN, tap("book:start"))

    text, keyboard = wa_sends["sent"][-1]
    assert "_flow" not in (keyboard or {})
    assert text == content.text("scheduling.no_slots")


async def test_without_a_published_flow_whatsapp_keeps_the_period_question(
        monkeypatch: pytest.MonkeyPatch, desk, wa_sends, times):
    _flows(monkeypatch, booking_id="")

    await handler.process_callback(CONN, tap("book:start"))

    assert wa_sends["sent"][-1][0] == content.text("scheduling.period_question")


async def test_telegram_keeps_the_period_question(monkeypatch: pytest.MonkeyPatch, desk, times):
    _flows(monkeypatch)

    await handler.process_callback(CONN, tap("book:start"))

    assert desk.sent[-1] == content.text("scheduling.period_question")


async def test_the_name_typed_in_the_flow_is_kept(
        monkeypatch: pytest.MonkeyPatch, desk, wa_sends, times):
    _flows(monkeypatch)
    await handler.process_callback(CONN, tap("book:start"))

    await handler.process_callback(CONN, _answer(desk.offer_calls, horario="0",
                                                 nome="  Ana   Lima "))

    assert desk.details.get("name") == "Ana Lima"
    assert "Ana Lima" in desk.created_events[0].title


@pytest.mark.parametrize("typed", ["invalido", "x ana@ok.com", 42])
async def test_without_an_e_mail_a_bad_one_books_nothing_and_sends_the_flow_again(
        monkeypatch: pytest.MonkeyPatch, desk, wa_sends, times, typed):
    _flows(monkeypatch)
    desk.known = {"phone": "+55 91 90000-0000"}
    await handler.process_callback(CONN, tap("book:start"))
    first = desk.offer_calls

    await handler.process_callback(CONN, _answer(first, horario="0", email=typed))

    assert desk.created_events == []
    text, keyboard = wa_sends["sent"][-1]
    assert text.startswith(content.text("booking.flow_bad_email"))
    assert keyboard["_flow"]["token"] == f"agendar:{desk.offer_calls}" != f"agendar:{first}"


async def test_with_an_e_mail_on_record_a_bad_one_still_books(
        monkeypatch: pytest.MonkeyPatch, desk, wa_sends, times):
    _flows(monkeypatch)
    await handler.process_callback(CONN, tap("book:start"))

    await handler.process_callback(CONN, _answer(desk.offer_calls, horario="0", email="??"))

    assert len(desk.created_events) == 1 and "email" not in desk.details


async def test_a_time_taken_meanwhile_offers_again_and_keeps_the_reschedule(
        monkeypatch: pytest.MonkeyPatch, desk, wa_sends, times, reminded):
    _flows(monkeypatch)
    await handler.process_callback(CONN, tap("book:start"))
    offer = desk.offers["42"].model_copy(update={"replaces_id": reminded.id})
    desk.offers["42"] = offer
    desk.busy = [times.starts[0]]

    await handler.process_callback(CONN, _answer(offer.id, horario="0"))

    assert desk.created_events == []
    assert wa_sends["sent"][-1][0].startswith(content.text("scheduling.slot_taken"))
    assert desk.offers["42"].id != offer.id and desk.offers["42"].replaces_id == reminded.id


@pytest.mark.parametrize("period, kept", [("morning", "morning"), ("any", None)])
async def test_the_three_times_remember_the_period_chosen(
        monkeypatch: pytest.MonkeyPatch, desk, period, kept):
    await handler.process_callback(CONN, tap(f"period:{period}:none:none"))

    assert desk.offers["42"].period == kept


# ── "Pedir mais informações": its buttons, when a person holds the conversation ──

from tests.test_desk_flow import _held_in_the_panel  # noqa: E402


@pytest.fixture
def info_request(monkeypatch: pytest.MonkeyPatch):
    """Request 4 of Ana (contact 7), waiting on a proposal unless told otherwise."""
    from datetime import datetime

    from core.models import ProposalBriefing
    from core.repositories import proposal_briefings as briefings

    state = {"status": "sent", "contact_id": 7}

    async def get(conn, briefing_id):
        if briefing_id != 4:
            return None
        now = datetime(2026, 11, 9, 12, 0)
        return ProposalBriefing(id=4, contact_id=state["contact_id"], status=state["status"],
                                answers={}, created_at=now, updated_at=now)

    monkeypatch.setattr(briefings, "get", get)
    return state


def _info_tap(data: str) -> dict:
    """The tap, on the message that carried the two buttons."""
    update = tap(data)
    update["callback_query"]["message"]["reply_markup"] = {"inline_keyboard": [
        [{"text": "Conversar por aqui", "callback_data": "propinfo:team:4"}],
        [{"text": "Agendar conversa", "callback_data": "propinfo:meet:4"}]]}
    return update


async def test_meet_from_the_information_request_hands_the_conversation_back_and_books(
        monkeypatch: pytest.MonkeyPatch, desk, times, info_request):
    _held_in_the_panel(desk)
    desk.owner_chats[2] = "777"

    await handler.process_callback(CONN, _info_tap("propinfo:meet:4"))

    assert ("777", content.render("inbox.owner_ping", nome="Ana")) in desk.posted, \
        "whoever asked hears it"
    assert desk.sessions[1]["status"] == "closed"
    assert desk.sent[-1] == content.text("scheduling.period_question")
    taps = [r for r in desk.recorded if r[0] == "user"]
    assert taps == [("user", "(tocou no botão) Agendar conversa", None)], "recorded once"


@pytest.mark.parametrize("status, owner", [("proposal_sent", 7), ("sent", 99)])
async def test_an_old_or_foreign_information_button_never_ends_a_conversation(
        monkeypatch: pytest.MonkeyPatch, desk, times, info_request, status, owner):
    info_request.update(status=status, contact_id=owner)
    _held_in_the_panel(desk)

    await handler.process_callback(CONN, _info_tap("propinfo:meet:4"))

    assert desk.sessions[1]["status"] == "claimed" and desk.sent == []


async def test_team_from_the_information_request_stays_with_the_person(
        monkeypatch: pytest.MonkeyPatch, desk, times, info_request):
    _held_in_the_panel(desk)

    await handler.process_callback(CONN, _info_tap("propinfo:team:4"))

    assert desk.sessions[1]["status"] == "claimed", "the person who asked keeps it"
    assert desk.sent == [], "the bot says nothing while a person holds it"
    assert desk.recorded and desk.recorded[0][2] == "handoff", "it reaches them in the panel"


async def test_the_information_request_buttons_work_without_anyone_holding_it(
        monkeypatch: pytest.MonkeyPatch, desk, times, info_request):
    await handler.process_callback(CONN, _info_tap("propinfo:meet:4"))

    assert desk.sent[-1] == content.text("scheduling.period_question")
