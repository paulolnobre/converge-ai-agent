"""Encryption at rest of the agent's data: the compose override that points
Postgres and Redis at the LUKS volume, and the scripts that unlock it at boot
with a key fetched from AWS. The scripts run here against fake binaries — no
disk, no network, no real key."""
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
OVERRIDE = DEPLOY / "docker-compose.encrypted.yml"


def _bind(service: str) -> list[dict]:
    data = yaml.safe_load(OVERRIDE.read_text())
    return [v for v in data["services"][service]["volumes"] if isinstance(v, dict)]


def test_the_override_binds_the_encrypted_volume():
    data = yaml.safe_load(OVERRIDE.read_text())
    assert set(data["services"]) == {"postgres", "redis", "reception-bot"}
    for service, source, target in (
        ("postgres", "/srv/converge-agent/postgres", "/var/lib/postgresql/data"),
        ("redis", "/srv/converge-agent/redis", "/data"),
        # The Inbox's kept media: customers' files never on the plain disk.
        ("reception-bot", "/srv/converge-agent/media", "/data/media"),
    ):
        assert set(data["services"][service]) == {"volumes"}, "the override changes nothing else"
        binds = _bind(service)
        assert binds == [{"type": "bind", "source": source, "target": target,
                          "bind": {"create_host_path": False}}], service


def test_the_base_compose_still_uses_named_volumes():
    base = (DEPLOY / "docker-compose.prod.yml").read_text()
    assert "- postgres_data:/var/lib/postgresql/data" in base
    assert "- redis_data:/data" in base


def test_ci_ships_the_override_and_the_luks_scripts():
    ci = (ROOT / ".github" / "workflows" / "ci-cd.yml").read_text()
    assert ('deploy/docker-compose.encrypted.yml "$VPS:/opt/converge-agent/'
            'docker-compose.encrypted.yml"') in ci
    assert 'deploy/luks/ "$VPS:/opt/converge-agent/luks/"' in ci


# ── Unlocking at boot ────────────────────────────────────────────────────────


LUKS = DEPLOY / "luks"
UNLOCK = LUKS / "converge-agent-unlock"
KEY = "chave-de-teste-nunca-no-disco"


@pytest.fixture
def box(tmp_path: Path):
    """A fake system: binaries that record what they were asked, a work dir
    for logs, and the settings file the script reads."""
    bin_dir, work = tmp_path / "bin", tmp_path / "work"
    bin_dir.mkdir()
    work.mkdir()
    (tmp_path / "mapper").mkdir()
    (tmp_path / "stack").mkdir()
    fakes = {
        "curl": 'echo "curl $*" >> "$WORK/calls"\n'
                'if [ -n "$FAKE_CURL_HANG" ]; then\n'
                '  t=$(echo "$*" | sed -E "s/.*--max-time ([0-9]+).*/\\1/")\n'
                '  /bin/sleep $(( t < 5 ? t : 5 )); exit 28\n'
                'fi\n'
                '[ -n "$FAKE_CURL_FAIL" ] && exit 22\n'
                f'printf \'{{"Parameter":{{"Value":"{KEY}"}}}}\'\n',
        "cryptsetup": 'echo "cryptsetup $*" >> "$WORK/calls"\n'
                      'cat > "$WORK/cryptsetup.stdin"\n',
        "mount": 'echo "mount $*" >> "$WORK/calls"\n',
        "mountpoint": 'echo "mountpoint $*" >> "$WORK/calls"\n'
                      '[ -n "$FAKE_MOUNTED" ] && exit 0\nexit 1\n',
        "findmnt": 'echo "findmnt $*" >> "$WORK/calls"\n'
                   '[ -n "$FAKE_MOUNTED_SOURCE" ] || exit 1\n'
                   'echo "$FAKE_MOUNTED_SOURCE"\n',
        "chattr": 'echo "chattr $*" >> "$WORK/calls"\n',
        "fallocate": 'echo "fallocate $*" >> "$WORK/calls"\ntouch "${@: -1}"\n',
        "mkfs.ext4": 'echo "mkfs.ext4 $*" >> "$WORK/calls"\n',
        "docker": 'echo "docker $*" >> "$WORK/calls"\necho "$PWD" >> "$WORK/docker.pwd"\n',
        "sleep": 'echo "sleep $*" >> "$WORK/calls"\n/bin/sleep "$1"\n',
    }
    for name, body in fakes.items():
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)
    settings = tmp_path / "luks.env"
    settings.write_text("AWS_ACCESS_KEY_ID=AKIAFAKE\nAWS_SECRET_ACCESS_KEY=fake-secret\n"
                        "AWS_DEFAULT_REGION=sa-east-1\nLUKS_SSM_PARAMETER=/converge/agent/luks-key\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "WORK": str(work),
           "LUKS_ENV": str(settings), "LUKS_IMAGE": str(tmp_path / "agent.img"),
           "LUKS_NAME": "converge-agent", "LUKS_MAPPER_DIR": str(tmp_path / "mapper"),
           "LUKS_MOUNT": str(tmp_path / "mnt"), "STACK_DIR": str(tmp_path / "stack"),
           "UNLOCK_BUDGET": "600", "UNLOCK_WAIT": "5"}
    return tmp_path, work, env


def _run(script: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script)], env=env, text=True,
                          capture_output=True, check=False, timeout=30)


def _calls(work: Path) -> list[str]:
    path = work / "calls"
    return path.read_text().splitlines() if path.exists() else []


def test_unlock_passes_the_key_only_through_stdin(box):
    root, work, env = box
    result = _run(UNLOCK, env)

    assert result.returncode == 0, result.stderr
    assert (work / "cryptsetup.stdin").read_text() == KEY
    opened = [c for c in _calls(work) if c.startswith("cryptsetup open")]
    assert opened == [f"cryptsetup open --type luks2 --key-file=- {root / 'agent.img'} converge-agent"]
    assert KEY not in result.stdout + result.stderr
    leaked = [p for p in root.rglob("*") if p.is_file() and p.name != "cryptsetup.stdin"
              and p.parent.name != "bin" and KEY in p.read_text(errors="ignore")]
    assert leaked == []


def test_unlock_asks_aws_for_the_parameter_with_sigv4(box):
    _, work, env = box
    _run(UNLOCK, env)
    curl = next(c for c in _calls(work) if c.startswith("curl"))
    assert "--aws-sigv4 aws:amz:sa-east-1:ssm" in curl
    assert "https://ssm.sa-east-1.amazonaws.com/" in curl
    assert "AmazonSSM.GetParameter" in curl and '"/converge/agent/luks-key"' in curl
    assert "--max-time 20" in curl and "-f" in curl.split()


def test_unlock_mounts_and_starts_the_stack(box):
    root, work, env = box
    _run(UNLOCK, env)
    calls = _calls(work)
    mount = f"mount -o noatime {root / 'mapper' / 'converge-agent'} {root / 'mnt'}"
    up = f"docker compose --project-directory {root / 'stack'} up -d"
    recreate = up + " --force-recreate postgres redis"
    assert mount in calls and recreate in calls and up in calls
    assert calls.index(mount) < calls.index(recreate) < calls.index(up)


def test_unlock_runs_compose_from_the_stack_dir(box):
    # COMPOSE_FILE in .env names the override by a relative path; systemd runs
    # the script from /.
    root, work, env = box
    _run(UNLOCK, env)
    assert set((work / "docker.pwd").read_text().split()) == {str(root / "stack")}


def test_unlock_refuses_to_mount_over_files_outside_the_volume(box):
    # A folder made on the root disk while the volume was shut would let
    # postgres start an empty, unencrypted database there.
    root, work, env = box
    (root / "mnt" / "postgres").mkdir(parents=True)
    result = _run(UNLOCK, env)

    assert result.returncode != 0
    calls = _calls(work)
    assert not any(c.startswith(("mount ", "docker")) for c in calls)
    assert "fora do volume" in result.stderr


def test_unlock_refuses_when_something_else_is_mounted_there(box):
    _, work, env = box
    result = _run(UNLOCK, {**env, "FAKE_MOUNTED_SOURCE": "/dev/sda1"})

    assert result.returncode != 0
    assert not any(c.startswith(("mount ", "docker", "cryptsetup")) for c in _calls(work))
    assert "/dev/sda1" in result.stderr


def test_unlock_locks_the_empty_mountpoint_before_mounting(box):
    root, work, env = box
    _run(UNLOCK, env)
    calls = _calls(work)
    lock = f"chattr +i {root / 'mnt'}"
    assert lock in calls
    assert calls.index(lock) < next(i for i, c in enumerate(calls) if c.startswith("mount "))


def test_unlock_when_already_mounted_only_starts_the_stack(box):
    root, work, env = box
    mapper = str(root / "mapper" / "converge-agent")
    result = _run(UNLOCK, {**env, "FAKE_MOUNTED_SOURCE": mapper})

    assert result.returncode == 0, result.stderr
    calls = _calls(work)
    assert not any(c.startswith(("curl", "cryptsetup", "mount ")) for c in calls)
    assert f"docker compose --project-directory {root / 'stack'} up -d" in calls


def test_unlock_when_already_open_only_mounts(box):
    root, work, env = box
    (root / "mapper" / "converge-agent").touch()
    result = _run(UNLOCK, env)

    assert result.returncode == 0, result.stderr
    calls = _calls(work)
    assert not any(c.startswith(("curl", "cryptsetup")) for c in calls)
    assert any(c.startswith("mount -o noatime") for c in calls)


def test_unlock_gives_up_after_its_budget(box):
    _, work, env = box
    started = time.monotonic()
    result = _run(UNLOCK, {**env, "FAKE_CURL_FAIL": "1", "UNLOCK_BUDGET": "3",
                           "UNLOCK_WAIT": "1"})

    assert result.returncode != 0
    # Bash counts whole seconds, so the deadline can land up to 1s early.
    assert time.monotonic() - started >= 2
    calls = _calls(work)
    assert len([c for c in calls if c.startswith("curl")]) <= 4
    assert not any(c.startswith("cryptsetup") for c in calls)
    assert "desist" in result.stderr.strip().splitlines()[-1].lower()


def test_unlock_gives_up_on_time_when_aws_hangs(box):
    _, work, env = box
    started = time.monotonic()
    result = _run(UNLOCK, {**env, "FAKE_CURL_HANG": "1", "UNLOCK_BUDGET": "3",
                           "UNLOCK_WAIT": "1"})

    assert result.returncode != 0
    assert time.monotonic() - started < 6
    for call in (c for c in _calls(work) if c.startswith("curl")):
        assert int(call.split("--max-time ")[1].split()[0]) <= 3, call


def test_the_unit_only_orders_after_docker():
    # The directives systemd reads — comments may name what the unit avoids.
    unit = "\n".join(line for line in (LUKS / "converge-agent-volume.service").read_text().splitlines()
                     if not line.lstrip().startswith("#"))
    assert "After=network-online.target docker.service" in unit
    assert "Wants=network-online.target" in unit
    for forbidden in ("Requires=", "BindsTo=", "Before=docker.service", "Requisite="):
        assert forbidden not in unit, forbidden
    assert "ExecStart=/usr/local/sbin/converge-agent-unlock" in unit
    assert "Type=oneshot" in unit and "RemainAfterExit=yes" in unit
    timeout = int(unit.split("TimeoutStartSec=")[1].split()[0])
    assert timeout >= 600 + 180


def test_create_volume_locks_the_mountpoint_before_mounting(box):
    root, work, env = box
    fake_id = root / "bin" / "id"
    fake_id.write_text("#!/usr/bin/env bash\necho 0\n")
    fake_id.chmod(0o755)
    result = _run(LUKS / "create-volume.sh", env)

    assert result.returncode == 0, result.stderr
    calls = _calls(work)
    lock = f"chattr +i {root / 'mnt'}"
    mount = next(i for i, c in enumerate(calls) if c.startswith("mount "))
    assert lock in calls and calls.index(lock) < mount
    assert (work / "cryptsetup.stdin").read_text() == KEY
    assert KEY not in result.stdout + result.stderr


def test_no_script_writes_the_key_to_disk():
    for script in (UNLOCK, LUKS / "create-volume.sh", LUKS / "install.sh"):
        text = script.read_text()
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if "Value" in line or "key-file" in line:
                assert " > " not in line and ">>" not in line and "tee " not in line, line
        for part in text.split("--key-file")[1:]:
            assert part.startswith("=-"), script.name


def test_create_volume_refuses_an_existing_image(box):
    root, work, env = box
    (root / "agent.img").write_text("já existe")
    result = _run(LUKS / "create-volume.sh", env)

    assert result.returncode != 0
    assert "já existe" in result.stderr
    assert not any(c.startswith("cryptsetup") for c in _calls(work))


@pytest.mark.parametrize("mounted", ["", "/dev/sda1"])
def test_create_volume_refuses_a_mountpoint_in_use(box, mounted):
    # Mounting over folders already there would hide them until a boot shows
    # them to postgres again — unencrypted.
    root, work, env = box
    if not mounted:
        (root / "mnt" / "postgres").mkdir(parents=True)
    result = _run(LUKS / "create-volume.sh", {**env, "FAKE_MOUNTED_SOURCE": mounted})

    assert result.returncode != 0
    calls = _calls(work)
    assert not any(c.startswith(("fallocate", "cryptsetup", "mkfs", "mount ", "chattr"))
                   for c in calls)
    assert not (root / "agent.img").exists()
    assert "nada foi feito" in result.stderr
