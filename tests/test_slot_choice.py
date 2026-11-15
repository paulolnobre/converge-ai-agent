"""Reading which of the offered times somebody just picked.

The cost of reading these two-word replies wrong is an appointment in a
stranger's diary, so the cases below are mostly about what the interpreter
refuses to decide.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.config import settings
from core.integrations.workspace.models import TimeSlot
from core.services import slot_choice

ZONE = ZoneInfo(settings.office_timezone)


def slot(day: int, hour: int, minute: int = 0) -> TimeSlot:
    start = datetime(2026, 9, day, hour, minute, tzinfo=ZONE)
    return TimeSlot(start=start, end=start + timedelta(minutes=30))


# Monday 14/09 at 10:00, Tuesday 15/09 at 14:00, Wednesday 16/09 at 09:00.
OFFER = [slot(14, 10), slot(15, 14), slot(16, 9)]
NOW = datetime(2026, 9, 13, 10, 0, tzinfo=ZONE)


def pick(message: str, slots=None):
    return slot_choice.interpret(message, slots if slots is not None else OFFER, now=NOW)


@pytest.mark.parametrize(
    "message, expected_day",
    [
        ("a terca", 15),
        ("terça", 15),
        ("terça-feira", 15),
        ("quarta", 16),
        ("10h", 14),
        ("as 9", 16),
        ("14:00", 15),
        ("o primeiro", 14),
        ("opcao 2", 15),
        ("segunda as 10", 14),
        ("15/09", 15),
        ("16/09 as 9", 16),
        ("2 da tarde", 15),
        ("perfeito, terça", 15),
        ("amanha", 14),
    ],
)
def test_a_clear_pick_is_read_as_one_slot(message: str, expected_day: int):
    choice = pick(message)
    assert choice.kind == "picked"
    assert choice.slot is not None and choice.slot.start.day == expected_day


def test_a_greeting_is_not_a_preference_for_the_afternoon():
    """"boa tarde" is how the message opens, not which half of the day."""
    choice = pick("boa tarde, pode ser quarta")
    assert choice.kind == "picked"
    assert choice.slot is not None and choice.slot.start.day == 16


def test_ter_the_verb_is_not_terca_the_day():
    """"vou ter que ver" is somebody stalling, not choosing Tuesday."""
    assert pick("vou ter que ver com meu chefe").kind == "unclear"


def test_segunda_is_monday_not_the_second_option():
    """In an offer that names weekdays, "segunda" can only be the day."""
    choice = pick("segunda")
    assert choice.kind == "picked"
    assert choice.slot is not None and choice.slot.start.day == 14


def test_a_day_with_two_offered_times_is_ambiguous_not_a_guess():
    two_on_tuesday = [slot(15, 9), slot(15, 14), slot(16, 9)]
    choice = pick("terca", two_on_tuesday)
    assert choice.kind == "ambiguous"
    assert len(choice.candidates) == 2


def test_a_period_narrows_without_deciding():
    choice = pick("de manha")
    assert choice.kind == "ambiguous"
    assert {candidate.start.day for candidate in choice.candidates} == {14, 16}


def test_agreement_alone_is_not_a_choice_between_three_times():
    """"pode ser" agrees with meeting, not with a Tuesday."""
    assert pick("pode ser").kind == "ambiguous"
    assert pick("sim").kind == "ambiguous"


def test_agreement_is_a_choice_when_only_one_time_was_offered():
    only = [slot(15, 14)]
    choice = pick("pode ser", only)
    assert choice.kind == "picked" and choice.slot == only[0]


def test_a_day_that_was_never_offered_is_a_refusal():
    """"quinta" against Mon/Tue/Wed is turning the offer down, not a misread."""
    assert pick("quinta").kind == "declined"


@pytest.mark.parametrize(
    "message",
    [
        "nenhum desses", "nao posso em nenhum", "outro dia", "mais tarde",
        "outras datas", "vc teria outras datas disponiveis?", "outros horarios",
        "tem outras horas?",
    ],
)
def test_plain_refusals_are_read_as_refusals(message: str):
    assert pick(message).kind == "declined"


def test_a_refusal_wins_over_a_date_that_matches_every_offered_slot():
    """All three offered times can fall on the same day (a re-offer after a
    reschedule); naming that exact date while declining must not read as
    picking among them."""
    same_day = [slot(14, 13, 30), slot(14, 15, 30), slot(14, 17, 30)]
    choice = pick("eu nao consigo dia 14/09", same_day)
    assert choice.kind == "declined"


def test_the_n_abbreviation_is_read_as_nao():
    same_day = [slot(14, 13, 30), slot(14, 15, 30), slot(14, 17, 30)]
    choice = pick("eu n consigo dia 14/09", same_day)
    assert choice.kind == "declined"


def test_a_bare_no_next_to_a_day_still_picks_the_day():
    """"nao pode ser terca?" is someone choosing Tuesday, politely."""
    choice = pick("nao pode ser terca?")
    assert choice.kind == "picked"
    assert choice.slot is not None and choice.slot.start.day == 15


# ── The day someone asks for (NOW is Sunday 13/09/2026) ─────────────────────

@pytest.mark.parametrize(
    "message, expected",
    [
        ("quinta", (2026, 9, 17)),
        ("acredito que na quinta feira", (2026, 9, 17)),
        ("na quinta feira 1/10", (2026, 10, 1)),      # the date wins
        ("01/10/2026", (2026, 10, 1)),
        ("dia 20", (2026, 9, 20)),
        ("dia 5", (2026, 10, 5)),                     # already past this month
        ("amanhã", (2026, 9, 14)),
        ("depois de amanha", (2026, 9, 15)),
        ("hoje", (2026, 9, 13)),
        ("semana que vem", (2026, 9, 14)),
        ("quinta da semana que vem", (2026, 9, 17)),
        ("tanto faz", (2026, 9, 13)),
        ("sem preferência", (2026, 9, 13)),
        ("amanhã, sem preferência de período", (2026, 9, 14)),
        ("sexta, sem preferência de horário", (2026, 9, 18)),
        ("amanhã, tanto faz o horário", (2026, 9, 14)),
        ("domingo", (2026, 9, 20)),                   # today's weekday is next week
    ],
)
def test_the_requested_day_is_read_in_code(message: str, expected):
    assert slot_choice.requested_day(message, now=NOW) == datetime(*expected).date()


def test_a_date_early_next_year_is_not_read_as_ten_months_ago():
    december = datetime(2026, 12, 10, 10, 0, tzinfo=ZONE)
    assert slot_choice.requested_day("05/01", now=december) == datetime(2027, 1, 5).date()


def test_a_recent_past_date_is_returned_for_the_caller_to_refuse():
    assert slot_choice.requested_day("10/09", now=NOW) == datetime(2026, 9, 10).date()


@pytest.mark.parametrize("message", ["quero marcar", "31/02"])
def test_no_day_or_an_impossible_one_is_none(message: str):
    assert slot_choice.requested_day(message, now=NOW) is None


def test_an_ordinal_outside_the_offer_is_not_rounded_to_the_nearest():
    assert pick("o quinto").kind == "unclear"


def test_a_question_in_the_middle_of_booking_is_not_a_pick():
    assert pick("quanto custa a consulta?").kind == "unclear"


def test_nothing_offered_decides_nothing():
    assert slot_choice.interpret("a terca", [], now=NOW).kind == "unclear"


def test_the_offered_time_is_read_in_the_office_zone():
    """Slots come back from JSON with an offset; the day must not shift."""
    utc = [
        TimeSlot(
            start=slot(15, 14).start.astimezone(ZoneInfo("UTC")),
            end=slot(15, 14).end.astimezone(ZoneInfo("UTC")),
        )
    ]
    choice = pick("terca as 14h", utc)
    assert choice.kind == "picked"


# ── Reading a typed day while the list of days is open ───────────────────────

from datetime import date as _date  # noqa: E402

from core.services.slot_choice import read_day_reply  # noqa: E402

_NOW = datetime(2026, 10, 19, 10, 0, tzinfo=ZoneInfo(settings.office_timezone))  # a Monday
_THU, _FRI = _date(2026, 10, 22), _date(2026, 10, 23)


@pytest.mark.parametrize("text, kind, days, period", [
    ("quinta", "clear", [_THU], None),
    ("quinta de manhã", "clear", [_THU], "morning"),
    ("quinta ou sexta", "clear", [_THU, _FRI], None),
    ("quinta, não tenho preferência de horário", "clear", [_THU], "any"),
    ("na quinta não tenho preferência de horário", "clear", [_THU], "any"),
    ("quinta, qualquer horário", "clear", [_THU], "any"),
    ("quinta não posso", "mixed", [], None),
    ("não posso quinta e sexta", "mixed", [], None),
    ("quinta não, sexta sim", "mixed", [], None),
    ("quinta não e sexta sim", "mixed", [], None),
    ("não posso quinta, mas sexta", "mixed", [], None),
    ("quinta n posso", "mixed", [], None),
    ("quinta n e sexta sim", "mixed", [], None),
    ("sem preferência de horário", "period_only", [], "any"),
    ("de tarde", "period_only", [], "afternoon"),
    ("não posso de manhã", "mixed", [], None),
    ("n posso de tarde", "mixed", [], None),
    ("quero cancelar a de quinta", "cancel_or_exit", [], None),
    ("obrigado", "none", [], None),
])
def test_read_day_reply(text, kind, days, period):
    reply = read_day_reply(text, now=_NOW)
    assert (reply.kind, reply.days, reply.period) == (kind, days, period), text


def test_read_day_reply_reads_dates_and_relative_days():
    assert read_day_reply("dia 22/10", now=_NOW).days == [_THU]
    assert read_day_reply("amanhã", now=_NOW).days == [_date(2026, 10, 20)]
    assert read_day_reply("sair", now=_NOW).kind == "cancel_or_exit"


# ── An hour without minutes means on the hour ────────────────────────────────

def _offer(*minutes_past_ten: int) -> list[TimeSlot]:
    zone = ZoneInfo(settings.office_timezone)
    return [TimeSlot(start=datetime(2026, 10, 22, 10, m, tzinfo=zone),
                     end=datetime(2026, 10, 22, 10, m, tzinfo=zone) + timedelta(minutes=30))
            for m in minutes_past_ten]


def test_an_hour_without_minutes_means_on_the_hour():
    page = _offer(0, 30)
    choice = slot_choice.interpret("às 10h", page, now=_NOW)
    assert choice.kind == "picked" and choice.slot == page[0]


def test_an_hour_without_minutes_and_only_the_half_hour_is_ambiguous():
    choice = slot_choice.interpret("às 10h", _offer(30), now=_NOW)
    assert choice.kind == "ambiguous"


@pytest.mark.parametrize("text, days", [
    ("quinta da semana que vem", [_date(2026, 10, 29)]),
    ("quinta 29/10", [_date(2026, 10, 29)]),
    ("semana que vem", [_date(2026, 10, 26)]),
    ("sim, pode ser quinta", [_THU]),
    ("sexta sim", [_FRI]),
    # Codex r2: a date overrides only the weekday said with it
    ("amanhã ou sexta", [_date(2026, 10, 20), _FRI]),
    ("quinta 29/10 ou sexta", [_date(2026, 10, 29), _FRI]),
    ("29/10, quinta", [_date(2026, 10, 29)]),
    ("quinta, dia 29", [_date(2026, 10, 29)]),
    # Codex r3: the "-feira" suffix
    ("quinta-feira 29/10", [_date(2026, 10, 29)]),
    ("29/10, quinta-feira", [_date(2026, 10, 29)]),
    ("quinta feira, dia 29", [_date(2026, 10, 29)]),
    ("quinta-feira ou sexta-feira", [_THU, _FRI]),
])
def test_read_day_reply_keeps_requested_days_rules(text, days):
    """The week said with a weekday, an explicit date over the weekday said
    with it, and a "sim" with no "não" is not a mixed answer."""
    reply = read_day_reply(text, now=_NOW)
    assert (reply.kind, reply.days) == ("clear", days), text


@pytest.mark.parametrize("text", ["quinta, sem ser de manhã", "qualquer dia exceto quinta",
                                  "menos sexta"])
def test_other_negatives_make_an_answer_mixed(text):
    assert read_day_reply(text, now=_NOW).kind == "mixed", text
