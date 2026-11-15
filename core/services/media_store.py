"""The files kept for the Inbox, on the encrypted volume (MEDIA_DIR).

Each file is sealed by the app before it touches the disk — the same envelope
as the database fields, bound to its own key (AAD "media:<key>"), so a file
copied under another name does not open. The path says nothing but that key.
Writes are atomic: a temporary in the same folder, fsync, then rename.
"""
from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from core.config import settings
from core.crypto import key_version_of, open_bytes, seal_bytes

_MIB = 1024 * 1024
_TEMPORARY = ".tmp-"
_ENVELOPE_HEAD = 30  # format, key version, nonce, tag: core.crypto's shortest envelope


@dataclass(frozen=True)
class Entry:
    path: Path
    key: UUID | None    # None for a temporary
    temporary: bool
    age_seconds: float


def _root() -> Path:
    return Path(settings.media_dir)


def _context(key: UUID) -> str:
    return f"media:{key}"


def path_for(key: UUID) -> Path:
    return _root() / key.hex[:2] / f"{key.hex}.bin"


def write(key: UUID, data: bytes) -> int:
    """Seal and write; returns the bytes on disk. On any failure the
    temporary is removed and the error raised."""
    sealed = seal_bytes(_context(key), data)
    target = path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f"{_TEMPORARY}{uuid.uuid4().hex}"
    try:
        with open(temporary, "wb") as handle:
            handle.write(sealed)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return len(sealed)


def read(key: UUID) -> bytes:
    """The original bytes. FileNotFoundError and SealedDataError go up."""
    return open_bytes(_context(key), path_for(key).read_bytes())


def version_at(path: Path) -> int:
    """The key version in a file's envelope header, without opening it.
    OSError and SealedDataError go up."""
    with open(path, "rb") as handle:
        return key_version_of(handle.read(_ENVELOPE_HEAD))


def key_version(key: UUID) -> int:
    return version_at(path_for(key))


def delete(key: UUID) -> bool:
    try:
        path_for(key).unlink()
    except FileNotFoundError:
        return False
    return True


def ensure_writable() -> None:
    """At startup with capture on: a folder the app cannot write would answer
    every customer's file with "não consegui receber". RuntimeError instead."""
    probe = _root() / f"{_TEMPORARY}startup-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"MEDIA_ENABLED=true, but MEDIA_DIR ({_root()}) cannot be written by this user "
            f"({type(exc).__name__}). On the VPS the folder must belong to the image's app "
            "user (docs/CRIPTOGRAFIA.md, \"Os arquivos do Inbox\").") from None


def free_bytes() -> int:
    stats = os.statvfs(_root())
    return stats.f_bavail * stats.f_frsize


def has_room(size: int, stored_total: int) -> bool:
    """Within MEDIA_MAX_TOTAL_MB, and MEDIA_MIN_FREE_MB still free after the
    write — the floor that keeps Postgres and Redis writing."""
    if stored_total + size > settings.media_max_total_mb * _MIB:
        return False
    return free_bytes() - size >= settings.media_min_free_mb * _MIB


def entries() -> Iterator[Entry]:
    """Every kept file and every temporary under MEDIA_DIR, with its age."""
    root, now = _root(), time.time()
    if not root.exists():
        return
    for path in root.glob("*/*"):
        try:
            if not path.is_file():
                continue
            age = now - path.stat().st_mtime
        except FileNotFoundError:
            continue  # renamed or removed since the listing
        if path.name.startswith(_TEMPORARY):
            yield Entry(path, None, True, age)
        elif path.suffix == ".bin":
            try:
                yield Entry(path, UUID(hex=path.stem), False, age)
            except ValueError:
                continue
