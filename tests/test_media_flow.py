"""What the desk does with a photo, a file or an audio once capture is on
(spec 2026-10-22, §2 and §4): the order in the turn, the request to the team,
the one notice, and every case where the bot stays quiet. The desk harness of
test_desk_flow, with the media repository and the download faked; the disk is
a temporary MEDIA_DIR."""
# ruff: noqa: F811
from datetime import UTC, datetime, timedelta

import pytest

from core import content
from core.config import settings
from core.handlers import message as handler
from core.integrations.media_download import Downloaded, Failed
from core.models import AgentReply, Triage
from core.repositories import inbox as inbox_repo
from core.repositories import media as media_repo
from core.services import media, media_store
from tests.test_desk_flow import (  # noqa: F401
    CONN,
    TEAM,
    WA_NUMBER,
    _held_in_the_panel,
    _meta,
    _value,
    desk,
    wa,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
OGG = b"OggS" + b"\x00" * 64
NOW = datetime.now(UTC)


class World:
    def __init__(self):
        self.known: dict[str, media_repo.NewMedia] = {}
        self.events: set[str] = set()
        self.fetched: list[str] = []
        self.serve: dict[str, object] = {}
        self.transcribed: list[bytes] = []
        self.triaged: list[str] = []


@pytest.fixture
def world(monkeypatch, desk, tmp_path):
    w = World()
    monkeypatch.setattr(settings, "media_enabled", True)
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    monkeypatch.setattr(media_store, "free_bytes", lambda: 10 * 1024**3)

    async def exists(conn, source_id):
        return source_id in w.known

    async def record(conn, line, new):
        if new.source_id in w.known:
            return None
        w.known[new.source_id] = new
        desk.recorded.append(("user", line, "media"))
        return len(w.known)

    async def event_seen(conn, update_id):
        return update_id in w.events

    async def mark_event(conn, update_id):
        w.events.add(update_id)

    for name, fn in {"exists": exists, "record": record, "event_seen": event_seen,
                     "mark_event": mark_event,
                     "stored_total": lambda conn: _value(0)}.items():
        monkeypatch.setattr(media_repo, name, fn)

    async def fetch_attachment(file_id, *, declared=None):
        w.fetched.append(file_id)
        return w.serve.get(file_id, Downloaded(JPEG, None))

    async def transcribe(data, mime=None, prompt=None):
        w.transcribed.append(data)
        return "quero marcar"

    async def route(body, chat_id, history_text="", organization="", retrieve=None):
        w.triaged.append(body)
        return Triage(intent="other", confidence=0.9), AgentReply(text="Claro.", intent="other")

    async def claim_once(conn, contact_id):
        # The real claim is one UPDATE: true once per conversation, then false.
        taken, desk.new_conversation = desk.new_conversation, False
        return taken

    monkeypatch.setattr(inbox_repo, "claim_conversation_notice", claim_once)
    monkeypatch.setattr(media.channels, "fetch_attachment", fetch_attachment)
    monkeypatch.setattr(handler.audio, "transcribe", transcribe)
    monkeypatch.setattr(handler, "route", route)
    return w


def _tg(update_id: int, *, edited: bool = False, date: datetime | None = None,
        **fields) -> dict:
    message = {"message_id": 9, "chat": {"id": 42, "type": "private"},
               "from": {"first_name": "Ana"}, "date": int((date or NOW).timestamp()), **fields}
    return {"update_id": update_id, ("edited_message" if edited else "message"): message}


def _photo(update_id: int = 100, unique: str = "U1", message_id: int = 9, **extra) -> dict:
    update = _tg(update_id, photo=[{"file_id": f"F-{unique}", "file_unique_id": unique,
                                    "file_size": len(JPEG)}], **extra)
    next(iter(v for k, v in update.items() if k != "update_id"))["message_id"] = message_id
    return update


def _voice(update_id: int = 200, duration: int = 3) -> dict:
    return _tg(update_id, voice={"file_id": "V1", "file_unique_id": "UV", "mime_type": "audio/ogg",
                                 "file_size": len(OGG), "duration": duration})


def _statuses(world: World) -> list[str]:
    return [new.status for new in world.known.values()]


async def test_a_photo_without_caption_opens_a_request_and_confirms(world, desk):
    assert await handler.process_message(CONN, _photo()) == "media"
    assert desk.sent == [content.text("media.received")]
    assert len(desk.sessions) == 1 and _statuses(world) == ["stored"]
    assert any("enviou uma foto" in text for _, text in desk.posted)
    assert not any("Nova conversa" in text for _, text in desk.posted)
    assert ("assistant", content.text("media.received"), "media") in desk.recorded


async def test_an_album_of_six_gives_one_request_one_notice_one_confirmation(world, desk):
    for n in range(6):
        await handler.process_message(CONN, _photo(100 + n, unique=f"U{n}", message_id=10 + n))
    assert desk.sent == [content.text("media.received")]
    assert len(desk.sessions) == 1 and len(world.known) == 6
    assert sum("enviou uma foto" in text for _, text in desk.posted) == 1


async def test_a_caption_goes_through_the_bot(world, desk):
    await handler.process_message(CONN, _photo(caption="quanto custa?"))
    assert world.triaged == ["quanto custa?"] and desk.sessions == {}
    assert _statuses(world) == ["stored"]


async def test_the_first_message_with_a_caption_still_announces_the_conversation(world, desk):
    desk.new_conversation = True
    await handler.process_message(CONN, _photo(caption="oi, segue"))
    assert sum("Nova conversa" in text for _, text in desk.posted) == 1


async def test_a_caption_that_is_a_command_still_captures(world, desk):
    await handler.process_message(CONN, _photo(caption="/id"))
    assert desk.sent == ["Seu ID: 42"] and _statuses(world) == ["stored"]


async def test_a_transcribed_audio_with_no_space_follows_the_transcript(world, desk, monkeypatch):
    world.serve["V1"] = Downloaded(OGG, "audio/ogg")
    monkeypatch.setattr(media_store, "has_room", lambda size, stored_total: False)
    await handler.process_message(CONN, _voice())
    assert _statuses(world) == ["no_space"]
    assert world.transcribed == [OGG] and world.triaged == ["quero marcar"]
    assert content.text("media.not_received") not in desk.sent


async def test_a_voice_hello_after_a_day_is_not_taken_for_a_conversation(
        world, desk, monkeypatch):
    """The audio's own line is recorded before it is transcribed: read after
    it, every voice "oi" would look like a conversation going on."""
    from core.repositories import contacts as contacts_repo

    world.serve["V1"] = Downloaded(OGG, "audio/ogg")
    reads: list[datetime] = []

    async def last_message_at(conn, chat_id):
        reads.append(NOW)
        return NOW - timedelta(hours=25) if len(reads) == 1 else NOW

    async def oi(data, mime=None, prompt=None):
        return "Oi, boa noite"

    monkeypatch.setattr(contacts_repo, "last_message_at", last_message_at)
    monkeypatch.setattr(handler.audio, "transcribe", oi)
    monkeypatch.setattr(handler, "greet_in_conversation",
                        lambda *a: pytest.fail("not a conversation going on"))

    await handler.process_message(CONN, _voice())

    assert desk.sent[-1] == content.render("reply.welcome_back", nome="Ana")
    assert len(reads) == 1, "read once, before the audio was recorded"


async def test_an_audio_is_downloaded_once(world, desk, monkeypatch):
    world.serve["V1"] = Downloaded(OGG, "audio/ogg")

    async def never(file_id):
        pytest.fail("the audio was already downloaded by the capture")

    monkeypatch.setattr(handler.channels, "download_voice", never)
    await handler.process_message(CONN, _voice())
    assert world.fetched == ["V1"] and world.transcribed == [OGG]


async def test_an_untranscribed_stored_audio_opens_a_request(world, desk, monkeypatch):
    world.serve["V1"] = Downloaded(OGG, "audio/ogg")

    async def nothing(data, mime=None, prompt=None):
        return None

    monkeypatch.setattr(handler.audio, "transcribe", nothing)
    await handler.process_message(CONN, _voice())
    assert desk.sent == [content.text("media.received")] and len(desk.sessions) == 1


async def test_a_long_audio_is_stored_but_not_transcribed(world, desk):
    world.serve["V1"] = Downloaded(OGG, "audio/ogg")
    await handler.process_message(CONN, _voice(duration=settings.max_voice_seconds + 1))
    assert _statuses(world) == ["stored"] and world.transcribed == []
    assert desk.sent == [content.text("media.received")]


async def test_not_stored_without_text_asks_to_resend(world, desk):
    world.serve["F-U1"] = Failed("status 500")
    await handler.process_message(CONN, _photo())
    assert desk.sent == [content.text("media.not_received")] and desk.sessions == {}
    assert _statuses(world) == ["failed"]


async def test_a_sticker_is_silent(world, desk):
    desk.new_conversation = True
    sticker = _tg(300, sticker={"file_id": "S1", "file_unique_id": "US", "file_size": 10})
    world.serve["S1"] = Downloaded(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8, None)
    await handler.process_message(CONN, sticker)
    assert desk.sent == [] and desk.sessions == {}
    assert sum("Nova conversa" in text for _, text in desk.posted) == 1


async def test_a_held_conversation_is_silent_and_the_owner_is_pinged(world, desk):
    _held_in_the_panel(desk)
    desk.owner_chats[2] = "222"
    await handler.process_message(CONN, _photo())
    assert desk.sent == [] and _statuses(world) == ["stored"]
    assert any(chat == "222" for chat, _ in desk.posted)


async def test_rate_limited_never_downloads(world, desk, monkeypatch):
    _held_in_the_panel(desk)
    monkeypatch.setattr(handler, "_allowed", lambda chat_id: _value((False, False)))
    assert await handler.process_message(CONN, _photo()) == "handoff"
    assert world.fetched == [] and _statuses(world) == ["rate_limited"]


async def test_a_caption_edit_while_rate_limited_and_held_keeps_the_file(world, desk, monkeypatch):
    await handler.process_message(CONN, _photo(caption="primeira"))
    _held_in_the_panel(desk)
    monkeypatch.setattr(handler, "_allowed", lambda chat_id: _value((False, False)))
    edit = _photo(101, caption="corrigida")
    edit["edited_message"] = edit.pop("message")
    await handler.process_message(CONN, edit)
    assert _statuses(world) == ["stored"] and world.fetched == ["F-U1"]
    assert ("user", "corrigida", "handoff") in desk.recorded


async def test_a_late_whatsapp_media_is_recorded_without_download(world, desk, wa):
    update = _meta("image", {"id": "M1", "mime_type": "image/jpeg"})
    update["message"]["date"] = int((NOW - timedelta(hours=25)).timestamp())
    assert await handler.process_message(CONN, update) == "expired"
    assert world.fetched == [] and _statuses(world) == ["late"] and desk.sent == []
    assert content.text("inbox.unreadable") not in [m for _, m, _ in desk.recorded]


async def test_replay_ends_the_turn_before_the_limit(world, desk, monkeypatch):
    world.events.add("100")

    async def never(chat_id):
        pytest.fail("a replay must not spend the rate limit")

    monkeypatch.setattr(handler, "_allowed", never)
    assert await handler.process_message(CONN, _photo(100)) == "replayed"
    assert world.fetched == [] and desk.sent == [] and desk.recorded == []


async def test_a_media_update_older_than_90_days_ends_the_turn(world, desk):
    old = _photo(date=NOW - timedelta(days=91))
    assert await handler.process_message(CONN, old) == "too_old"
    assert world.fetched == [] and desk.sent == []


async def test_a_telegram_caption_edit_of_an_audio_is_text_only(world, desk):
    world.serve["A1"] = Downloaded(OGG, "audio/ogg")
    audio = {"file_id": "A1", "file_unique_id": "UA", "mime_type": "audio/mpeg", "file_size": 10,
             "duration": 3}
    await handler.process_message(CONN, _tg(400, audio=audio))
    fetched, transcribed = list(world.fetched), list(world.transcribed)
    await handler.process_message(CONN, _tg(401, edited=True, audio=audio, caption="corrigido"))
    assert world.fetched == fetched and world.transcribed == transcribed
    assert world.triaged[-1] == "corrigido"


async def test_a_caption_removed_by_an_edit_is_silent(world, desk):
    await handler.process_message(CONN, _photo(caption="primeira"))
    sent = list(desk.sent)
    edit = _photo(102)
    edit["edited_message"] = edit.pop("message")
    assert await handler.process_message(CONN, edit) is None
    assert desk.sent == sent and len(world.known) == 1


async def test_an_edit_with_a_new_file_captures_again(world, desk):
    await handler.process_message(CONN, _photo(caption="primeira"))
    edit = _photo(103, unique="U2", caption="primeira")
    edit["edited_message"] = edit.pop("message")
    await handler.process_message(CONN, edit)
    assert len(world.known) == 2 and world.fetched == ["F-U1", "F-U2"]


async def test_capture_off_is_today(world, desk, wa, monkeypatch):
    update = _meta("image", {"id": "M1", "mime_type": "image/jpeg", "caption": "meu exame"})
    assert "_media" in update["message"]  # converted with capture on
    monkeypatch.setattr(settings, "media_enabled", False)
    await handler.process_message(CONN, update)
    assert desk.sent == [handler._CANNOT_READ] and world.triaged == [] and world.known == {}


async def test_capture_off_telegram_photo_is_today(world, desk, monkeypatch):
    monkeypatch.setattr(settings, "media_enabled", False)
    await handler.process_message(CONN, _photo())
    assert desk.sent == [handler._CANNOT_READ] and world.fetched == [] and world.events == set()


async def test_capture_off_still_ends_a_replay(world, desk, monkeypatch):
    monkeypatch.setattr(settings, "media_enabled", False)
    world.events.add("100")
    assert await handler.process_message(CONN, _photo(100)) == "replayed"


async def test_a_telegram_contact_is_created_before_capture(world, desk):
    await handler.process_message(CONN, _photo())
    new, = world.known.values()
    assert new.contact_id == 7 and new.chat_id == "42"


async def test_capture_off_still_marks_a_pending_media_event(world, desk, wa, monkeypatch):
    """Spec §2, 1a: a payload with _media is marked in the turn's transaction
    with capture on or off — a worker that falls between the commit and the
    ACK must not handle it twice."""
    update = _meta("image", {"id": "M1", "mime_type": "image/jpeg", "caption": "meu exame"})
    monkeypatch.setattr(settings, "media_enabled", False)
    await handler.process_message(CONN, update)
    assert str(update["update_id"]) in world.events
    assert await handler.process_message(CONN, update) == "replayed"


async def test_a_caption_edit_as_the_first_message_still_announces_the_conversation(world, desk):
    await handler.process_message(CONN, _photo(caption="primeira"))
    desk.new_conversation = True  # a day later: a new conversation
    edit = _photo(104, caption="corrigida")
    edit["edited_message"] = edit.pop("message")
    await handler.process_message(CONN, edit)
    assert sum("Nova conversa" in text for _, text in desk.posted) == 1


async def test_a_capture_undone_by_an_sql_error_still_announces_the_conversation(
        world, desk, monkeypatch):
    async def undone(*args, **kwargs):
        return None

    monkeypatch.setattr(media, "capture", undone)
    desk.new_conversation = True
    await handler.process_message(CONN, _photo(caption="oi"))
    assert sum("Nova conversa" in text for _, text in desk.posted) == 1


async def test_a_blocked_contacts_audio_is_not_downloaded(world, desk, monkeypatch):
    from core.repositories import contacts as contacts_repo

    monkeypatch.setattr(contacts_repo, "is_blocked", lambda conn, chat_id: _value(True))
    world.serve["V1"] = Downloaded(OGG, "audio/ogg")

    assert await handler.process_message(CONN, _voice()) == "blocked"

    assert world.fetched == [] and world.transcribed == [] and desk.sent == []
    assert ("user", content.text("media.line_audio"), "blocked") in desk.recorded
