"""Capturing what a customer sent: downloaded once, sealed to disk, recorded
with its own line, and every way it can fail kept as a status — never as an
exception that takes the turn down. The repository is faked; the disk is real
(a temporary MEDIA_DIR)."""
import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from core import content
from core.config import settings
from core.integrations.media_download import Downloaded, Failed, TooLarge
from core.models import Contact
from core.repositories import media as media_repo
from core.services import media, media_store
from core.services.media import SAME_ATTACHMENT, Captured

CONN = object()
ANA = Contact(id=7, chat_id="42", name="Ana")
NOW = datetime.now(UTC)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
OGG = b"OggS" + b"\x00" * 64


def _photo(**extra) -> dict:
    return {"message_id": 9, "chat": {"id": 42, "type": "private"}, "date": int(NOW.timestamp()),
            "photo": [{"file_id": "F1", "file_unique_id": "U1", "file_size": len(JPEG)}], **extra}


def _voice() -> dict:
    return {"message_id": 10, "chat": {"id": 42, "type": "private"}, "date": int(NOW.timestamp()),
            "voice": {"file_id": "V1", "file_unique_id": "UV", "file_size": len(OGG),
                      "mime_type": "audio/ogg", "duration": 3}}


class Repo:
    def __init__(self):
        self.known: set[str] = set()
        self.recorded: list[tuple[str, media_repo.NewMedia]] = []
        self.total = 0
        self.conflict = False
        self.error = False
        self.events: set[str] = set()

    async def exists(self, conn, source_id):
        return source_id in self.known

    async def stored_total(self, conn):
        return self.total

    async def record(self, conn, line, new):
        if self.error:
            raise psycopg.errors.SyntaxError("boom")
        if self.conflict:
            return None
        self.recorded.append((line, new))
        return len(self.recorded)

    async def event_seen(self, conn, update_id):
        return update_id in self.events

    async def mark_event(self, conn, update_id):
        self.events.add(update_id)


@pytest.fixture
def repo(monkeypatch, tmp_path):
    fake = Repo()
    for name in ("exists", "stored_total", "record", "event_seen", "mark_event"):
        monkeypatch.setattr(media_repo, name, getattr(fake, name))
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    monkeypatch.setattr(settings, "media_enabled", True)
    monkeypatch.setattr(media_store, "free_bytes", lambda: 10 * 1024**3)
    return fake


def _serve(monkeypatch, result) -> list:
    asked = []

    async def fetch_attachment(file_id, *, declared=None):
        asked.append(file_id)
        return result

    monkeypatch.setattr(media.channels, "fetch_attachment", fetch_attachment)
    return asked


async def test_a_photo_is_downloaded_sealed_and_recorded_once(monkeypatch, repo):
    asked = _serve(monkeypatch, Downloaded(JPEG, "image/jpeg"))
    result = await media.capture(CONN, ANA, 42, _photo(), edited=False)
    assert result == Captured(kind="image", status="stored", audio=None)
    assert asked == ["F1"]
    (line, new), = repo.recorded
    assert line == content.text("media.line_image") == "(enviou uma foto)"
    assert new.source_id == "tg:42:9:U1" and new.mime == "image/jpeg"
    assert new.stored_bytes == len(JPEG) and new.storage_key is not None
    assert new.stored_at is not None and new.hold_until is None
    assert new.expires_at == new.stored_at + timedelta(days=90)
    assert media_store.read(new.storage_key) == JPEG


async def test_an_audio_keeps_its_bytes_for_the_transcript(monkeypatch, repo):
    _serve(monkeypatch, Downloaded(OGG, "audio/ogg"))
    result = await media.capture(CONN, ANA, 42, _voice(), edited=False)
    assert result == Captured(kind="audio", status="stored", audio=OGG)
    (_, new), = repo.recorded
    assert new.stored_at is not None
    assert new.hold_until == new.stored_at + timedelta(hours=48)
    assert new.expires_at == new.stored_at + timedelta(hours=168)


async def test_a_known_attachment_is_the_same_attachment(monkeypatch, repo):
    repo.known.add("tg:42:9:U1")
    asked = _serve(monkeypatch, Downloaded(JPEG, None))
    assert await media.capture(CONN, ANA, 42, _photo(), edited=True) is SAME_ATTACHMENT
    assert asked == [] and repo.recorded == []


@pytest.mark.parametrize("fetched, room, status", [
    (TooLarge(25 * 1024 * 1024), True, "too_large"),
    (Failed("status 500"), True, "failed"),
    (Downloaded(JPEG, None), False, "no_space"),
])
async def test_too_large_failed_and_no_space_are_recorded_without_a_file(
        monkeypatch, repo, fetched, room, status):
    _serve(monkeypatch, fetched)
    monkeypatch.setattr(media_store, "has_room", lambda size, stored_total: room)
    result = await media.capture(CONN, ANA, 42, _photo(), edited=False)
    assert isinstance(result, Captured) and result.status == status
    (_, new), = repo.recorded
    assert new.storage_key is None and new.stored_at is None and new.stored_bytes is None
    assert new.hold_until is None
    assert abs((new.expires_at - NOW) - timedelta(days=90)) < timedelta(minutes=5)
    if status == "too_large":
        assert new.declared_bytes == 25 * 1024 * 1024


async def test_an_oserror_on_write_is_failed_and_the_audio_still_has_bytes(monkeypatch, repo):
    _serve(monkeypatch, Downloaded(OGG, "audio/ogg"))

    def full(key, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(media_store, "write", full)
    result = await media.capture(CONN, ANA, 42, _voice(), edited=False)
    assert result == Captured(kind="audio", status="failed", audio=OGG)


async def test_cancellation_during_capture_is_not_swallowed(monkeypatch, repo):
    async def hang(file_id, *, declared=None):
        await asyncio.sleep(10)

    monkeypatch.setattr(media.channels, "fetch_attachment", hang)
    task = asyncio.create_task(media.capture(CONN, ANA, 42, _photo(), edited=False))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_conflict_on_insert_is_the_same_attachment_and_the_file_an_orphan(
        monkeypatch, repo, tmp_path):
    _serve(monkeypatch, Downloaded(JPEG, None))
    repo.conflict = True
    assert await media.capture(CONN, ANA, 42, _photo(), edited=False) is SAME_ATTACHMENT
    assert len([e for e in media_store.entries() if not e.temporary]) == 1  # left for the sweep


async def test_an_sql_error_leaves_the_turn_as_today(monkeypatch, repo):
    _serve(monkeypatch, Downloaded(JPEG, None))
    repo.error = True
    assert await media.capture(CONN, ANA, 42, _photo(), edited=False) is None


async def test_guard_replayed_too_old_and_marked(monkeypatch, repo):
    update = {"update_id": 501, "message": _photo()}
    repo.events.add("501")
    assert await media.guard_event(CONN, update, update["message"]) == "replayed"
    old = _photo(date=int((NOW - timedelta(days=91)).timestamp()))
    assert await media.guard_event(CONN, {"update_id": 502, "message": old}, old) == "too_old"
    assert "502" not in repo.events
    assert await media.guard_event(CONN, {"update_id": 503, "message": _photo()}, _photo()) is None
    assert "503" in repo.events


async def test_guard_with_capture_off(monkeypatch, repo):
    monkeypatch.setattr(settings, "media_enabled", False)
    old = _photo(date=int((NOW - timedelta(days=91)).timestamp()))
    assert await media.guard_event(CONN, {"update_id": 601, "message": old}, old) is None
    assert "601" not in repo.events  # read only: today's behaviour
    repo.events.add("602")
    assert await media.guard_event(CONN, {"update_id": 602, "message": old}, old) == "replayed"


async def test_an_edit_counts_from_its_edit_date(monkeypatch, repo):
    edited = _photo(date=int((NOW - timedelta(days=100)).timestamp()),
                    edit_date=int(NOW.timestamp()))
    assert await media.guard_event(CONN, {"update_id": 701, "edited_message": edited},
                                   edited) is None


async def test_a_text_message_is_not_guarded(repo):
    text = {"message_id": 1, "chat": {"id": 42}, "text": "oi"}
    assert await media.guard_event(CONN, {"update_id": 801, "message": text}, text) is None
    assert repo.events == set()


async def test_normalized_strips_media_and_the_whatsapp_caption_when_off(monkeypatch, repo):
    wa = {"chat": {"id": "wa:5591", "type": "private"}, "caption": "meu exame",
          "_media": {"kind": "image", "file_id": "wa-media:M", "mime_type": None,
                     "filename": None, "declared_bytes": None, "source_id": "wa:W:M"}}
    assert media.normalized(wa) == wa  # capture on: unchanged
    monkeypatch.setattr(settings, "media_enabled", False)
    assert media.normalized(wa) == {"chat": {"id": "wa:5591", "type": "private"}}
    tg = _photo(caption="olha")
    assert media.normalized(tg) == tg  # Telegram captions were always read


async def test_record_without_download_keeps_an_existing_attachment(monkeypatch, repo):
    from core.services.media_types import attachment_of
    found = attachment_of(_photo())
    assert found is not None
    repo.known.add(found.source_id)
    await media.record_without_download(CONN, ANA, 42, found, "rate_limited")
    assert repo.recorded == []
    repo.known.clear()
    await media.record_without_download(CONN, ANA, 42, found, "late")
    (line, new), = repo.recorded
    assert new.status == "late" and new.storage_key is None and line == "(enviou uma foto)"


def test_deadlines():
    t = datetime(2030, 1, 1, tzinfo=UTC)
    assert media.deadlines("audio", stored_at=t, created_at=t) == (
        t + timedelta(hours=48), t + timedelta(hours=168))
    assert media.deadlines("image", stored_at=t, created_at=t) == (None, t + timedelta(days=90))
    assert media.deadlines("audio", stored_at=None, created_at=t) == (None, t + timedelta(days=90))
