"""Files kept for the Inbox: sealed before they touch the disk, written
atomically, named only by their key, and kept within the space limits."""
import os
import time
import uuid

import pytest

from core.config import settings
from core.crypto import SealedDataError
from core.services import media_store

MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    return tmp_path


def _temporaries(root) -> list:
    return [p for p in root.rglob(".tmp-*")]


def test_written_bytes_read_back_and_are_sealed_on_disk(media_dir):
    key, data = uuid.uuid4(), b"%PDF-1.7 conteudo do exame"
    written = media_store.write(key, data)
    assert media_store.read(key) == data
    on_disk = media_store.path_for(key).read_bytes()
    assert b"conteudo do exame" not in on_disk and written == len(on_disk)
    assert _temporaries(media_dir) == []


def test_a_file_copied_under_another_key_does_not_open(media_dir):
    a, b = uuid.uuid4(), uuid.uuid4()
    media_store.write(a, b"segredo")
    target = media_store.path_for(b)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(media_store.path_for(a).read_bytes())
    with pytest.raises(SealedDataError):
        media_store.read(b)


def test_a_failed_rename_leaves_no_temporary(media_dir, monkeypatch):
    def refuse(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(media_store.os, "replace", refuse)
    with pytest.raises(OSError):
        media_store.write(uuid.uuid4(), b"x")
    assert _temporaries(media_dir) == []


def test_the_path_says_nothing_but_the_key(media_dir):
    key = uuid.uuid4()
    path = media_store.path_for(key)
    assert path == media_dir / key.hex[:2] / f"{key.hex}.bin"


def test_a_missing_file_raises_and_delete_says_so(media_dir):
    key = uuid.uuid4()
    with pytest.raises(FileNotFoundError):
        media_store.read(key)
    assert media_store.delete(key) is False
    media_store.write(key, b"x")
    assert media_store.delete(key) is True and not media_store.path_for(key).exists()


def test_room_respects_the_total_and_the_free_floor(monkeypatch):
    monkeypatch.setattr(settings, "media_max_total_mb", 1)
    monkeypatch.setattr(settings, "media_min_free_mb", 2048)
    monkeypatch.setattr(media_store, "free_bytes", lambda: 4096 * MIB)
    assert media_store.has_room(400 * 1024, stored_total=500 * 1024) is True
    assert media_store.has_room(600 * 1024, stored_total=500 * 1024) is False
    monkeypatch.setattr(settings, "media_max_total_mb", 3072)
    monkeypatch.setattr(media_store, "free_bytes", lambda: 2048 * MIB + 10)
    assert media_store.has_room(11, stored_total=0) is False
    assert media_store.has_room(10, stored_total=0) is True


def test_entries_lists_files_and_temporaries_with_age(media_dir):
    key = uuid.uuid4()
    media_store.write(key, b"x")
    stray = media_dir / "ab" / ".tmp-left"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"y")
    old = time.time() - 7200
    os.utime(stray, (old, old))
    found = {(e.key, e.temporary): e for e in media_store.entries()}
    assert found[(key, False)].age_seconds < 60
    assert found[(None, True)].age_seconds >= 7200 - 5


def test_free_bytes_reads_the_volume(media_dir):
    assert media_store.free_bytes() > 0


def test_a_file_gone_during_the_scan_is_skipped(monkeypatch, tmp_path):
    """A temporary renamed by a capture, or a file removed by /teste, between
    listing and stat: the scan goes on (sweep, status and purge use it)."""
    from pathlib import Path

    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    kept, gone = uuid.uuid4(), uuid.uuid4()
    media_store.write(kept, b"a")
    media_store.write(gone, b"b")
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        if self.name == f"{gone.hex}.bin":
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    assert [entry.key for entry in media_store.entries()] == [kept]


def test_startup_refuses_a_media_folder_it_cannot_write(monkeypatch, tmp_path):
    """With capture on, a folder the app user cannot write would answer every
    customer's photo with "não consegui receber": refuse to start instead."""
    import os

    from core.config import validate_startup_config

    folder = tmp_path / "media"
    folder.mkdir()
    folder.chmod(0o500)
    monkeypatch.setattr(settings, "media_dir", str(folder))
    monkeypatch.setattr(settings, "media_enabled", True)
    try:
        if os.access(folder, os.W_OK):
            pytest.skip("running as a user that writes anywhere")
        with pytest.raises(RuntimeError, match="MEDIA_DIR"):
            validate_startup_config()
        monkeypatch.setattr(settings, "media_enabled", False)
        validate_startup_config()  # capture off: the folder is not written
    finally:
        folder.chmod(0o700)
