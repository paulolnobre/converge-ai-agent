"""The cryptography behind signing in to /painel, without a server or a database."""
import pyotp
import pytest
from cryptography.fernet import Fernet

from core.config import settings
from core.services import panel_auth as auth


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    value = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "panel_secret_key", value)
    return value


def test_passwords_are_argon2id_and_verify_only_themselves():
    stored = auth.hash_password("uma-senha-bem-longa")
    assert stored.startswith("$argon2id$")
    assert auth.verify_password(stored, "uma-senha-bem-longa")
    assert not auth.verify_password(stored, "outra-senha-longa")


def test_no_account_never_verifies_whatever_is_typed():
    assert not auth.verify_password(None, "qualquer-coisa")
    assert not auth.verify_password("not-a-hash", "qualquer-coisa")


@pytest.mark.parametrize(("password", "ok"), [
    ("curta", False),
    ("ana-da-silva-2026", False),      # contains the e-mail's name
    ("aaaaaaaaaaaaaaaa", False),       # long but repetitive
    ("verde-cadeira-nuvem", True),
])
def test_new_passwords(password, ok):
    assert (auth.password_problem(password, "ana-da-silva@x.com") is None) is ok


def test_the_totp_secret_is_encrypted_and_only_this_key_opens_it(key, monkeypatch):
    secret = auth.new_totp_secret()
    sealed = auth.encrypt(secret)
    assert secret not in sealed and auth.decrypt(sealed) == secret

    monkeypatch.setattr(settings, "panel_secret_key", Fernet.generate_key().decode())
    with pytest.raises(auth.NotConfigured):
        auth.decrypt(sealed)


def test_a_missing_or_malformed_key_means_not_configured(monkeypatch):
    for value in ("", "not-a-key"):
        monkeypatch.setattr(settings, "panel_secret_key", value)
        assert not auth.configured()
        with pytest.raises(auth.NotConfigured):
            auth.encrypt("x")


def test_codes_are_accepted_one_step_either_side_and_no_further():
    secret = pyotp.random_base32()
    now = 1_800_000_000
    totp = pyotp.TOTP(secret)
    current = int(now // 30)
    assert auth.totp_step(secret, totp.at(now), now) == current
    assert auth.totp_step(secret, totp.at(now - 30), now) == current - 1
    assert auth.totp_step(secret, totp.at(now + 30), now) == current + 1
    assert auth.totp_step(secret, totp.at(now + 90), now) is None
    assert auth.totp_step(secret, "12345", now) is None
    assert auth.totp_step(secret, totp.at(now)[:3] + " " + totp.at(now)[3:], now) == current


def test_recovery_codes_are_unique_and_typed_any_way_match():
    codes = auth.new_recovery_codes()
    assert len(codes) == len(set(codes)) == 10
    code = codes[0]
    assert auth.recovery_digest(code) == auth.recovery_digest(code.upper().replace("-", " "))
    assert auth.recovery_digest(code) != auth.recovery_digest(codes[1])


def test_tokens_are_long_and_stored_only_as_a_hash():
    token = auth.new_token()
    assert len(token) >= 40
    digest = auth.digest(token)
    assert len(digest) == 64 and token not in digest


def test_the_qr_is_drawn_here_as_inline_svg():
    uri = auth.provisioning_uri(pyotp.random_base32(), "ana@x.com")
    assert uri.startswith("otpauth://totp/Converge:ana%40x.com?")
    assert auth.qr_data_uri(uri).startswith("data:image/svg+xml")
