"""The field encryption: what the database keeps is bytes, never the text."""
import base64
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

from core import crypto
from core.config import settings
from core.crypto import SealedDataError, fingerprint, open_sealed, open_text, seal, seal_text

ROOT = Path(__file__).resolve().parent.parent
KEY_2 = base64.b64encode(bytes(range(100, 132))).decode()


def test_round_trip_keeps_text_empty_accents_and_emoji():
    for text in ["", "Olá, João", "📅 amanhã às 9h"]:
        assert open_sealed("sessions.message", seal("sessions.message", text)) == text


def test_none_stays_none():
    assert seal("contacts.name", None) is None
    assert open_sealed("contacts.name", None) is None


def test_the_same_text_seals_differently():
    assert seal("contacts.name", "Maria") != seal("contacts.name", "Maria")


def test_the_envelope_carries_format_and_key_version():
    box = seal("contacts.name", "Maria")
    assert box is not None
    assert box[0] == 1 and box[1] == 1 and crypto.key_version_of(box) == 1
    assert b"Maria" not in box


def test_a_tampered_truncated_or_moved_envelope_is_refused_without_the_text():
    box = seal("contacts.phone", "5591988887777")
    assert box is not None
    tampered = box[:-1] + bytes([box[-1] ^ 1])
    for bad, context in [(tampered, "contacts.phone"), (box[:29], "contacts.phone"),
                         (box, "contacts.name")]:
        with pytest.raises(SealedDataError) as caught:
            open_sealed(context, bad)
        rendered = "".join(traceback.format_exception(caught.value))
        assert "5591988887777" not in rendered
        # Only our error: no InvalidTag (or anything else) chained under it.
        assert "During handling" not in rendered and "direct cause" not in rendered


def test_an_old_key_still_opens_after_rotation(monkeypatch):
    old = seal("contacts.name", "Maria")
    first = settings.data_keys
    monkeypatch.setattr(settings, "data_keys", f"2:{KEY_2},{first}")
    crypto.reset_keys()
    assert open_sealed("contacts.name", old) == "Maria"
    new = seal("contacts.name", "Maria")
    assert new is not None and crypto.key_version_of(new) == 2


def test_an_unknown_key_version_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "data_keys", f"2:{KEY_2}")
    crypto.reset_keys()
    from_two = seal("contacts.name", "Maria")
    monkeypatch.setattr(settings, "data_keys", f"1:{KEY_2}")
    crypto.reset_keys()
    with pytest.raises(SealedDataError):
        open_sealed("contacts.name", from_two)


def test_a_phone_has_one_fingerprint_in_any_format():
    assert fingerprint("phone", "+55 (91) 98888-7777") == fingerprint("phone", "91988887777")
    assert fingerprint("phone", "91988887777") == fingerprint("phone", "5591988887777")
    assert fingerprint("phone", None) is None


def test_an_email_fingerprint_trims_lowercases_and_refuses_inner_spaces():
    assert fingerprint("email", " Ana@X.com ") == fingerprint("email", "ana@x.com")
    assert fingerprint("email", "a b@x.com") is None


def test_kinds_never_collide():
    assert fingerprint("email", "5591988887777") != fingerprint("phone", "5591988887777")


def test_a_fingerprint_does_not_reveal_the_value():
    value = fingerprint("phone", "5591988887777")
    assert value is not None and len(value) == 32 and b"5591988887777" not in value


def test_the_redis_text_form_round_trips_as_ascii():
    text = seal_text("queue", '{"a":"ção"}')
    assert text.isascii() and "ção" not in text
    assert open_text("queue", text) == '{"a":"ção"}'


def test_data_keys_check_never_prints_a_key():
    result = subprocess.run(
        [sys.executable, "-m", "cli.data_keys", "check"],
        cwd=ROOT, capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "DATA_KEYS": f"1:{KEY_2}",
             "DATA_INDEX_KEY": KEY_2, "APP_ENV": "development"},
    )
    assert result.returncode == 0, result.stderr
    assert "escreve com a versão 1" in result.stdout and "DATA_INDEX_KEY: ok" in result.stdout
    assert KEY_2 not in result.stdout + result.stderr


def test_the_host_script_writes_both_keys_once_and_prints_none(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("IMAGE_TAG=x\n")
    script = ROOT / "deploy" / "data-keys.sh"
    first = subprocess.run(["bash", str(script), str(env_file)], capture_output=True,
                           text=True, check=False)
    assert first.returncode == 0, first.stderr
    lines = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    keys = lines["DATA_KEYS"].removeprefix("1:"), lines["DATA_INDEX_KEY"]
    assert lines["DATA_KEYS"].startswith("1:")
    assert all(len(key) == 44 and len(base64.b64decode(key)) == 32 for key in keys)
    assert not any(key in first.stdout + first.stderr for key in keys)
    again = subprocess.run(["bash", str(script), str(env_file)], capture_output=True,
                           text=True, check=False)
    assert again.returncode != 0
    assert env_file.read_text().count("DATA_KEYS=") == 1


def test_the_host_script_starts_a_new_line_after_a_file_without_one(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("IMAGE_TAG=x")
    result = subprocess.run(["bash", str(ROOT / "deploy" / "data-keys.sh"), str(env_file)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert env_file.read_text().splitlines()[0] == "IMAGE_TAG=x"


def test_ci_ships_the_host_key_script():
    ci = (ROOT / ".github" / "workflows" / "ci-cd.yml").read_text()
    assert 'deploy/data-keys.sh "$VPS:/opt/converge-agent/data-keys.sh"' in ci


def test_bytes_round_trip_in_the_same_envelope():
    sealed = crypto.seal_bytes("media:k", b"\x00\xffabc")
    assert crypto.open_bytes("media:k", sealed) == b"\x00\xffabc"
    assert b"abc" not in sealed
    assert crypto.key_version_of(sealed) == crypto.current_key_version()


def test_bytes_sealed_for_one_file_do_not_open_as_another():
    with pytest.raises(SealedDataError):
        crypto.open_bytes("media:b", crypto.seal_bytes("media:a", b"x"))
