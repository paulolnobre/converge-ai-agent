"""The one door every message to a customer goes through: Telegram stays
exactly as it was, and a "wa:" chat gets WhatsApp's lists and text."""
import logging

import pytest

from core import channels, content
from core.config import settings
from core.integrations import telegram, whatsapp
from core.services import attendance

WA = "wa:5591999990000"
NUMBER = "5591999990000"


class Calls:
    def __init__(self):
        self.log: list[tuple] = []
        self.result: str = "ok"

    def of(self, name: str) -> list[tuple]:
        return [c[1:] for c in self.log if c[0] == name]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch):
    rec = Calls()
    monkeypatch.setattr(settings, "whatsapp_access_token", "t")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "PN1")
    monkeypatch.setattr(settings, "whatsapp_app_secret", "s")
    monkeypatch.setattr(settings, "whatsapp_webhook_verify_token", "v")

    async def send_message(chat_id, text, **kwargs):
        rec.log.append(("tg_send", chat_id, text, kwargs))
        return True

    async def send_typing(chat_id, **kwargs):
        rec.log.append(("tg_typing", chat_id))

    async def download_voice(file_id, **kwargs):
        rec.log.append(("tg_voice", file_id))
        return b"TG"

    async def send_text(to, text):
        rec.log.append(("wa_text", to, text))
        return rec.result

    async def send_buttons(to, body, buttons):
        rec.log.append(("wa_buttons", to, body, buttons))
        return "ok"

    async def send_list(to, body, button_label, rows):
        rec.log.append(("wa_list", to, body, button_label, rows))
        return "ok"

    async def send_flow(to, body, **kwargs):
        rec.log.append(("wa_flow", to, body, kwargs))
        return rec.result

    async def mark_read_typing(message_id):
        rec.log.append(("wa_typing", message_id))
        return True

    async def download_media(media_id, max_bytes):
        rec.log.append(("wa_media", media_id, max_bytes))
        return b"OGG", "audio/ogg"

    for name, fn in {"send_message": send_message, "send_typing": send_typing,
                     "download_voice": download_voice}.items():
        monkeypatch.setattr(telegram, name, fn)
    for name, fn in {"send_text": send_text, "send_buttons": send_buttons,
                     "send_list": send_list, "send_flow": send_flow,
                     "mark_read_typing": mark_read_typing,
                     "download_media": download_media}.items():
        monkeypatch.setattr(whatsapp, name, fn)
    return rec


def _keyboard(n: int) -> dict:
    return telegram.inline_keyboard([[(f"Opção {i}", f"opt:{i}")] for i in range(n)])


async def test_a_telegram_chat_goes_to_telegram_unchanged(calls):
    kb = _keyboard(2)

    assert await channels.send(42, "oi", kb) is True

    assert calls.of("tg_send") == [(42, "oi", {"parse_mode": "Markdown", "reply_markup": kb})]
    assert not [c for c in calls.log if c[0].startswith("wa_")]


async def test_three_choices_are_a_list_too(calls):
    """Every choice on WhatsApp is a list, however few: one way to choose."""
    from datetime import UTC, datetime, timedelta

    from core.models import Appointment

    start = datetime(2026, 10, 27, 13, 30, tzinfo=UTC)
    meeting = Appointment(id=7, calendar_id="cal", provider="google", title="Conversa",
                          starts_at=start, ends_at=start + timedelta(minutes=30))
    await channels.send(WA, "Lembrete", attendance.reminder_keyboard(meeting))

    assert calls.of("wa_buttons") == []
    (to, body, label, rows), = calls.of("wa_list")
    assert (to, body, label) == (NUMBER, "Lembrete", "Escolher")
    assert [title for title, _ in rows] == [
        content.text("scheduling.reminder_confirm"), content.text("scheduling.reminder_reschedule"),
        content.text("scheduling.reminder_cancel")]
    assert _plain([data for _, data in rows]) == ["appt:ok:7", "appt:move:7", "appt:cancel:7"]
    assert calls.of("tg_send") == []


async def test_two_choices_are_a_list(calls):
    await channels.send(WA, "Qual?", _keyboard(2))

    assert calls.of("wa_buttons") == []
    (_, _, label, rows), = calls.of("wa_list")
    assert label == "Escolher" and rows == [("Opção 0", "opt:0"), ("Opção 1", "opt:1")]


async def test_four_to_ten_become_a_list(calls):
    await channels.send(WA, "Qual?", _keyboard(4))

    (to, body, label, rows), = calls.of("wa_list")
    assert (to, body, label) == (NUMBER, "Qual?", "Escolher")
    assert rows == [(f"Opção {i}", f"opt:{i}") for i in range(4)]


async def test_more_than_ten_keeps_ten_and_logs(calls, caplog):
    with caplog.at_level(logging.WARNING):
        await channels.send(WA, "Qual?", _keyboard(12))

    (_, _, _, rows), = calls.of("wa_list")
    assert len(rows) == 10
    assert "12" in caplog.text


async def test_titles_fit_whatsapps_limits(calls):
    long = "x" * 30
    await channels.send(WA, "a", telegram.inline_keyboard([[(long, "a")]]))
    await channels.send(WA, "b", telegram.inline_keyboard([[(long, str(i))] for i in range(4)]))

    assert calls.of("wa_buttons") == []
    assert calls.of("wa_list")[0][3] == [("x" * 24, "a")]
    assert {len(title) for title, _ in calls.of("wa_list")[1][3]} == {24}


def test_markdown_becomes_whatsapp_text():
    assert channels.to_whatsapp_text("*a* _b_ `c` [Abrir](https://x.io)") == (
        "*a* _b_ c Abrir: https://x.io"
    )


async def test_plain_text_goes_as_it_is(calls):
    await channels.send(WA, "`c` [x](y)", parse_mode=None)

    assert calls.of("wa_text") == [(NUMBER, "`c` [x](y)")]


async def test_a_link_button_becomes_text(calls):
    await channels.send(WA, "Nova conversa", telegram.link_keyboard("Abrir", "https://x.io"))

    assert calls.of("wa_text") == [(NUMBER, "Nova conversa\n\nAbrir: https://x.io")]


async def test_long_body_with_buttons_goes_in_two_messages(calls):
    body = "y" * 1100

    assert await channels.deliver(WA, body, _keyboard(2)) == "ok"

    assert calls.of("wa_text") == [(NUMBER, body)]
    assert calls.of("wa_list")[0][1] == content.text("whatsapp.choose_prompt")

    calls.result = "failed"
    assert await channels.deliver(WA, body, _keyboard(2)) == "failed"


async def test_text_is_cut_to_whatsapps_limit(calls):
    await channels.send(WA, "z" * 5000)

    assert len(calls.of("wa_text")[0][1]) == 4096


async def test_window_closed_passes_through(calls):
    calls.result = "window_closed"

    assert await channels.deliver(WA, "oi") == "window_closed"
    assert await channels.send(WA, "oi") is False


async def test_typing_per_channel(calls):
    await channels.typing(42)
    await channels.typing(WA, "wamid.1")
    await channels.typing(WA)

    assert calls.of("tg_typing") == [(42,)]
    assert calls.of("wa_typing") == [("wamid.1",)]


async def test_download_voice_per_channel(calls):
    assert await channels.download_voice("wa-media:m1") == b"OGG"
    assert await channels.download_voice("tg-file") == b"TG"

    assert calls.of("wa_media") == [("m1", settings.max_voice_bytes)]
    assert calls.of("tg_voice") == [("tg-file",)]


async def test_whatsapp_off_means_failed_without_a_call(calls, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "whatsapp_access_token", None)

    assert await channels.deliver(WA, "oi") == "failed"
    assert calls.log == []


def test_channel_of():
    assert channels.channel_of(WA) == "whatsapp"
    assert channels.channel_of(42) == "telegram"
    assert channels.is_whatsapp(WA) and not channels.is_whatsapp("42")


def test_a_message_older_than_the_window_is_expired_only_on_whatsapp():
    import time

    old = {"date": int(time.time()) - 25 * 3600}
    fresh = {"date": int(time.time()) - 60}

    assert channels.expired(WA, old) is True
    assert channels.expired(WA, fresh) is False
    assert channels.expired(WA, {}) is False          # no stamp: now
    assert channels.expired(42, old) is False         # Telegram has no window
    assert abs(channels.sent_at(fresh).timestamp() - fresh["date"]) < 1


def _day_menu(n: int) -> dict:
    return telegram.menu([[telegram.Choice(f"Terça, 2{i}/10", f"day:2026-10-2{i}:-:-",
                                           f"{i + 3} horários")] for i in range(n)],
                         list_button="Escolher dia")


async def test_a_menu_with_a_list_button_is_always_a_whatsapp_list(calls):
    await channels.send(WA, "Escolha o dia da conversa:", _day_menu(2))

    (to, body, label, rows), = calls.of("wa_list")
    assert (to, label) == (NUMBER, "Escolher dia")
    assert rows == [("Terça, 20/10", "day:2026-10-20:-:-", "3 horários"),
                    ("Terça, 21/10", "day:2026-10-21:-:-", "4 horários")]
    assert calls.of("wa_buttons") == []


async def test_telegram_shows_label_and_description_and_nothing_private(calls):
    await channels.send(42, "Escolha:", _day_menu(1))

    (_, _, sent), = calls.of("tg_send")
    button = sent["reply_markup"]["inline_keyboard"][0][0]
    assert button == {"text": "Terça, 20/10 · 3 horários", "callback_data": "day:2026-10-20:-:-"}
    assert "_list_button" not in sent["reply_markup"]


async def test_a_menu_description_is_cut_to_whatsapps_limit(calls):
    menu = telegram.menu([[telegram.Choice("Dia", "d", "y" * 100)]], list_button="Escolher")
    await channels.send(WA, "x", menu)

    (_, _, _, rows), = calls.of("wa_list")
    assert rows == [("Dia", "d", "y" * 72)]


def _plain(datas):
    """Buttons as their action and appointment, without the time stamp of the
    new format: appt2:move:1:29889694 → appt:move:1."""
    from core.services import appointment_taps

    out = []
    for data in datas:
        if data.startswith(appointment_taps.APPOINTMENT_PREFIXES):
            action, appointment_id, _ = appointment_taps.parse(data)
            out.append(f"appt:{action}:{appointment_id}")
        else:
            out.append(data)
    return out


# ── A Flow in place of the questions ────────────────────────────────────────

def _flow(**extra) -> dict:
    return channels.flow_keyboard(flow_id="F1", token="proposta:7", cta="Responder",
                                  screen="PROPOSTA", data={"quem": "Ana", "email": ""}, **extra)


async def test_a_whatsapp_chat_gets_the_flow(calls):
    assert await channels.deliver(WA, "Vamos montar a sua proposta", _flow()) == "ok"
    assert calls.of("wa_flow") == [(NUMBER, "Vamos montar a sua proposta", {
        "flow_id": "F1", "token": "proposta:7", "cta": "Responder", "screen": "PROPOSTA",
        "data": {"quem": "Ana", "email": ""}})]
    assert calls.of("wa_text") == [] and calls.of("wa_list") == []


async def test_telegram_never_gets_the_flow(calls):
    await channels.deliver("123", "Vamos montar a sua proposta", _flow())
    assert calls.of("wa_flow") == []
    chat_id, text, kwargs = calls.of("tg_send")[0]
    assert kwargs["reply_markup"] is None, "no empty keyboard for Telegram to refuse"
    assert "flow_id" not in str(kwargs)


async def test_a_refused_flow_says_so(calls):
    calls.result = "window_closed"
    assert await channels.deliver(WA, "Vamos montar a sua proposta", _flow()) == "window_closed"
