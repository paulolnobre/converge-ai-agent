"""deploy/push-keys.sh: the VAPID pair is made on the server, written to .env
once, and never shown."""
import base64
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "push-keys.sh"


def _run(env_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), str(env_file)], capture_output=True, text=True,
                          check=False)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_writes_a_working_pair_once_and_prints_neither(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("IMAGE_TAG=x\n")
    first = _run(env_file)
    assert first.returncode == 0, first.stderr
    lines = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    public, private = lines["PUSH_VAPID_PUBLIC_KEY"], lines["PUSH_VAPID_PRIVATE_KEY"]
    assert lines["PUSH_VAPID_SUBJECT"] == "mailto:team@getconverge.me"
    # The private key signs, and the public key is its own (what browsers verify).
    vapid = Vapid.from_string(private_key=private)
    assert vapid.public_key is not None
    derived = vapid.public_key.public_bytes(serialization.Encoding.X962,
                                            serialization.PublicFormat.UncompressedPoint)
    assert _b64(derived) == public and len(derived) == 65 and derived[0] == 4
    assert "=" not in public + private
    assert private not in first.stdout + first.stderr
    again = _run(env_file)
    assert again.returncode != 0
    assert env_file.read_text().count("PUSH_VAPID_PRIVATE_KEY=") == 1


def test_starts_a_new_line_after_a_file_without_one(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("IMAGE_TAG=x")
    assert _run(env_file).returncode == 0
    assert env_file.read_text().splitlines()[0] == "IMAGE_TAG=x"


def test_ci_ships_the_script():
    ci = (ROOT / ".github" / "workflows" / "ci-cd.yml").read_text()
    assert 'deploy/push-keys.sh "$VPS:/opt/converge-agent/push-keys.sh"' in ci


def test_fills_the_empty_lines_of_the_example(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("IMAGE_TAG=x\nPUSH_VAPID_PUBLIC_KEY=\nPUSH_VAPID_PRIVATE_KEY=\nPUSH_VAPID_SUBJECT=\nZ=1\n")
    result = _run(env_file)
    assert result.returncode == 0, result.stderr
    text = env_file.read_text()
    for name in ("PUBLIC_KEY", "PRIVATE_KEY", "SUBJECT"):
        assert text.count(f"PUSH_VAPID_{name}=") == 1
    lines = dict(line.split("=", 1) for line in text.splitlines())
    assert lines["PUSH_VAPID_PRIVATE_KEY"] and lines["Z"] == "1" and lines["IMAGE_TAG"] == "x"


def test_a_tail_that_writes_in_pieces_never_breaks_the_pipe(tmp_path):
    """On the server, 1 run in 10 died of SIGPIPE: `head -c 32` closed the pipe
    while `tail` was still writing. A tail that writes in pieces makes it
    happen every time."""
    import os
    import shutil
    real = shutil.which("tail")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "tail").write_text(
        "#!/usr/bin/env bash\n"
        'out="$(mktemp)"\n'
        f'"{real}" "$@" > "$out" || exit $?\n'
        'size=$(stat -c %s "$out")\n'
        'for ((i = 0; i < size; i += 8)); do\n'
        '  dd if="$out" bs=1 skip=$i count=8 status=none || exit $?; sleep 0.002\n'
        'done\n'
        'rm -f "$out"\n')
    (fake / "tail").chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("IMAGE_TAG=x\n")
    result = subprocess.run(["bash", str(SCRIPT), str(env_file)], capture_output=True, text=True,
                            check=False, env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})
    assert result.returncode == 0, (result.returncode, result.stderr)
    lines = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    vapid = Vapid.from_string(private_key=lines["PUSH_VAPID_PRIVATE_KEY"])
    assert vapid.public_key is not None
    derived = vapid.public_key.public_bytes(serialization.Encoding.X962,
                                            serialization.PublicFormat.UncompressedPoint)
    assert _b64(derived) == lines["PUSH_VAPID_PUBLIC_KEY"]
