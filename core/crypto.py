"""Encryption of the customer fields and of the queue, by the app itself.

The database keeps bytes: a leaked database password, an SQL injection or a
pg_dump in the wrong place give envelopes, not conversations. The keys live in
the environment (DATA_KEYS, DATA_INDEX_KEY), never in the database.

An envelope is ``format (1) ‖ key version (1) ‖ nonce (12) ‖ ciphertext ‖ tag
(16)``, AES-256-GCM. The context ("contacts.phone", "queue") is authenticated
with it, so a value moved into another column does not open. Nothing here ever
puts the text, the envelope or a key in an exception or a log line.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.config import parse_data_keys, parse_index_key, settings

_FORMAT = 1
_HEADER = 2
_NONCE = 12
_TAG = 16
_MIN_LENGTH = _HEADER + _NONCE + _TAG
_FINGERPRINT_PREFIX = b"converge-fp-v1\x00"

_keys: dict[int, bytes] | None = None
_write_version: int | None = None
_index_key: bytes | None = None


class SealedDataError(Exception):
    """An envelope that does not open. Never carries the content."""


def reset_keys() -> None:
    """Forget the cached keys; the next call reads the settings again."""
    global _keys, _write_version, _index_key
    _keys, _write_version, _index_key = None, None, None


def _load() -> tuple[dict[int, bytes], int]:
    global _keys, _write_version
    if _keys is None or _write_version is None:
        if not settings.data_keys:
            raise SealedDataError("DATA_KEYS is not set.")
        keys = parse_data_keys(settings.data_keys)
        _keys, _write_version = keys, next(iter(keys))
    return _keys, _write_version


def _index() -> bytes:
    global _index_key
    if _index_key is None:
        if not settings.data_index_key:
            raise SealedDataError("DATA_INDEX_KEY is not set.")
        _index_key = parse_index_key(settings.data_index_key)
    return _index_key


def seal(context: str, text: str | None) -> bytes | None:
    if text is None:
        return None
    keys, version = _load()
    nonce = os.urandom(_NONCE)
    body = AESGCM(keys[version]).encrypt(nonce, text.encode(), context.encode())
    return bytes([_FORMAT, version]) + nonce + body


def open_sealed(context: str, data: bytes | None) -> str | None:
    if data is None:
        return None
    data = bytes(data)
    if len(data) < _MIN_LENGTH or data[0] != _FORMAT:
        raise SealedDataError("Not an envelope of a known format.")
    keys, _ = _load()
    key = keys.get(data[1])
    if key is None:
        raise SealedDataError(f"Key version {data[1]} is not loaded.")
    nonce, body = data[_HEADER:_HEADER + _NONCE], data[_HEADER + _NONCE:]
    try:
        return AESGCM(key).decrypt(nonce, body, context.encode()).decode()
    except (InvalidTag, UnicodeDecodeError):
        raise SealedDataError("The envelope does not open (tampered or another field).") from None


def seal_bytes(context: str, data: bytes) -> bytes:
    """The same envelope as `seal`, over bytes (a file kept for the Inbox)."""
    keys, version = _load()
    nonce = os.urandom(_NONCE)
    return bytes([_FORMAT, version]) + nonce + AESGCM(keys[version]).encrypt(
        nonce, data, context.encode())


def open_bytes(context: str, data: bytes) -> bytes:
    data = bytes(data)
    if len(data) < _MIN_LENGTH or data[0] != _FORMAT:
        raise SealedDataError("Not an envelope of a known format.")
    keys, _ = _load()
    key = keys.get(data[1])
    if key is None:
        raise SealedDataError(f"Key version {data[1]} is not loaded.")
    try:
        return AESGCM(key).decrypt(data[_HEADER:_HEADER + _NONCE], data[_HEADER + _NONCE:],
                                   context.encode())
    except InvalidTag:
        raise SealedDataError("The envelope does not open (tampered or another file).") from None


def current_key_version() -> int:
    """The version new envelopes are written with (the first in DATA_KEYS)."""
    return _load()[1]


def key_version_of(data: bytes) -> int:
    data = bytes(data)
    if len(data) < _MIN_LENGTH or data[0] != _FORMAT:
        raise SealedDataError("Not an envelope of a known format.")
    return data[1]


def seal_text(context: str, text: str) -> str:
    """The same envelope as ASCII, for Redis (decode_responses=True)."""
    sealed = seal(context, text)
    assert sealed is not None
    return base64.b64encode(sealed).decode()


def open_text(context: str, data: str) -> str:
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        raise SealedDataError("Not an envelope of a known format.") from None
    opened = open_sealed(context, raw)
    assert opened is not None
    return opened


def canonical_phone(value: str) -> str | None:
    """55 + DDD + number, digits only, for a Brazilian phone; otherwise the
    digits as given. The same form for writing, looking up and converting."""
    from core.services.contact_details import _normalise_phone

    normalised = _normalise_phone(value)
    if normalised is not None:
        return re.sub(r"\D", "", normalised)
    digits = re.sub(r"\D", "", value)
    return digits or None


def canonical_email(value: str) -> str | None:
    """Trimmed and lower-cased; an address with a space inside is not one."""
    trimmed = value.strip()
    if not trimmed or re.search(r"\s", trimmed):
        return None
    return trimmed.lower()


def fingerprint(kind: Literal["phone", "email"], value: str | None) -> bytes | None:
    if value is None:
        return None
    canonical = canonical_phone(value) if kind == "phone" else canonical_email(value)
    if canonical is None:
        return None
    message = _FINGERPRINT_PREFIX + kind.encode() + b"\x00" + canonical.encode()
    return hmac.new(_index(), message, hashlib.sha256).digest()
