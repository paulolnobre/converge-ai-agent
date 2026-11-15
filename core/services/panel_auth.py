"""The cryptography behind signing in to /painel — pure functions, no database.

What is stored, and why none of it is enough on its own to get in:

  password        Argon2id hash. Slow by design; a leaked table is not a list
                  of passwords.
  TOTP secret     Encrypted (Fernet: AES-128-CBC + HMAC-SHA256) with
                  PANEL_SECRET_KEY, which lives only in the server's .env. The
                  database alone cannot produce a single code.
  session, invite and recovery tokens
                  Only their SHA-256. They are 256-bit random values, so a
                  plain hash is enough, and a leaked row cannot be replayed.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import pyotp
import segno
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from core.config import settings

ISSUER = "Converge"
MIN_PASSWORD = 12
_TOTP_PERIOD = 30
# Readable when typed from paper: no 0/o, 1/l/i.
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

_hasher = PasswordHasher()
# Verified against when the account does not exist, so "no such e-mail" takes
# as long as "wrong password" and the timing does not tell them apart.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


class NotConfigured(RuntimeError):
    """PANEL_SECRET_KEY is missing or not a valid key."""


# ── Passwords ────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY_HASH, password) and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    return _hasher.check_needs_rehash(stored)


def password_problem(password: str, email: str) -> str | None:
    """Why a new password is refused, in the words shown to the person."""
    if len(password) < MIN_PASSWORD:
        return f"A senha precisa ter pelo menos {MIN_PASSWORD} caracteres."
    local = email.split("@")[0].lower()
    if len(local) >= 4 and local in password.lower():
        return "A senha não pode conter o seu e-mail."
    if len(set(password)) < 5:
        return "A senha é repetitiva demais."
    return None


# ── Random tokens, stored only as hashes ─────────────────────────────────────

def new_token() -> str:
    return secrets.token_urlsafe(32)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def device_name(user_agent: str) -> str:
    """A short label, never the arbitrary User-Agent text itself."""
    platform = next((label for needle, label in (
        ("iPad", "iPad"), ("iPhone", "iPhone"), ("Android", "Android"),
        ("Windows", "Windows"), ("Macintosh", "Mac"), ("Linux", "Linux"),
    ) if needle in user_agent), "Aparelho")
    browser = next((label for needle, label in (
        ("Edg", "Edge"), ("OPR/", "Opera"), ("FxiOS", "Firefox"),
        ("Firefox", "Firefox"), ("CriOS", "Chrome"), ("Chrome", "Chrome"),
        ("Safari", "Safari"),
    ) if needle in user_agent), "Navegador")
    return f"{platform} · {browser}"


def new_recovery_codes(count: int = 10) -> list[str]:
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return codes


def recovery_digest(code: str) -> str:
    """Typed with or without the dashes, in any case — the same code."""
    return digest("".join(ch for ch in code.lower() if ch.isalnum()))


# ── The TOTP secret, encrypted at rest ───────────────────────────────────────

def _fernet() -> Fernet:
    key = settings.panel_secret_key.strip()
    if not key:
        raise NotConfigured("PANEL_SECRET_KEY is not set.")
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise NotConfigured("PANEL_SECRET_KEY is not a valid Fernet key.") from exc


def configured() -> bool:
    try:
        _fernet()
    except NotConfigured:
        return False
    return True


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        # A different key than the one it was encrypted with: the person has
        # to enroll again — nothing here can recover it, which is the point.
        raise NotConfigured("This secret was encrypted with another key.") from exc


# ── One-time codes ───────────────────────────────────────────────────────────

def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def qr_data_uri(uri: str) -> str:
    """The enrollment QR as an inline SVG. Drawn here, not by a QR web service:
    the URI carries the secret itself."""
    return segno.make(uri, error="m").svg_data_uri(scale=6, border=2)


def totp_step(secret: str, code: str, now: float | None = None) -> int | None:
    """The 30-second step this code belongs to, or None if it is not valid.

    One step either side is accepted for clock drift. The step is returned, not
    a yes/no, so the caller can refuse a code already used — a code seen over
    someone's shoulder must not work a second time inside its window.
    """
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    current = int((now if now is not None else time.time()) // _TOTP_PERIOD)
    for step in (current, current - 1, current + 1):
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None
