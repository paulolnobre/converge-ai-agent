"""Backups: a restic snapshot of the database and the server-only files, with nothing plaintext on disk."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SECRETS = ("aws-s3cr3t-value", "restic-passw0rd-value")


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def server(tmp_path: Path):
    """A stack directory, a settings file and fake restic/docker/curl/date on PATH."""
    stack = tmp_path / "opt" / "converge-agent"
    (stack / "content").mkdir(parents=True)
    shutil.copy2(DEPLOY / "backup.sh", stack / "backup.sh")
    (stack / ".env").write_text("TELEGRAM_BOT_TOKEN=abc\n")
    (stack / "content" / "staff.json").write_text("[]\n")
    (stack / "google_credentials.json").write_text("")  # empty: not worth keeping

    config = tmp_path / "backup.env"
    config.write_text(
        "RESTIC_REPOSITORY=s3:s3.sa-east-1.amazonaws.com/converge-backups/vps\n"
        f"RESTIC_PASSWORD={SECRETS[1]}\n"
        "AWS_ACCESS_KEY_ID=AKIAEXAMPLE\n"
        f"AWS_SECRET_ACCESS_KEY={SECRETS[0]}\n"
        "BACKUP_PING_URL=https://hc-ping.test/key\n"
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _executable(bin_dir / "restic", (
        'echo "restic $*" >> "$CALLS"\n'
        'echo "restic-env password=${RESTIC_PASSWORD:-missing} aws=${AWS_SECRET_ACCESS_KEY:-missing}"'
        ' >> "$CALLS"\n'
        'if [ "$FAIL" = db ] && [[ "$*" == *"--tag db"* ]]; then exit 1; fi\n'
        'if [ "$FAIL" = forget ] && [ "$1" = forget ]; then exit 1; fi\n'
    ))
    _executable(bin_dir / "curl", 'echo "curl ${!#}" >> "$CALLS"\n')
    _executable(bin_dir / "date", (
        'if [ "$1" = +%u ]; then echo "$WEEKDAY"; else exec /usr/bin/date "$@"; fi\n'
    ))

    def run(*args: str, fail: str = "", weekday: int = 3) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CALLS": str(log),
            "FAIL": fail,
            "WEEKDAY": str(weekday),
            "BACKUP_CONFIG": str(config),
            "BACKUP_LOCK": str(tmp_path / "backup.lock"),
        }
        return subprocess.run(
            ["bash", str(stack / "backup.sh"), str(stack), *args],
            text=True, capture_output=True, env=env, check=False,
        )

    def calls() -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    return stack, config, run, calls


def _restic(calls: list[str]) -> list[str]:
    return [line for line in calls if line.startswith("restic ")]


def test_the_database_is_streamed_into_a_snapshot_that_fails_with_pg_dump(server) -> None:
    stack, _, run, calls = server
    result = run("daily")
    assert result.returncode == 0, result.stderr

    db = next(line for line in _restic(calls()) if "--tag db" in line)
    assert "--tag converge-agent --tag daily" in db
    assert "--stdin-filename converge-agent-db.dump" in db
    # restic runs pg_dump itself, so a failed dump fails the snapshot, and the
    # dump never lands on this disk. Uncompressed, so restic can deduplicate it.
    assert "--stdin-from-command -- docker compose exec -T postgres sh -c" in db
    assert "pg_dump" in db and "-Fc -Z0" in db


def test_the_server_only_files_get_their_own_snapshot(server) -> None:
    stack, _, run, calls = server
    assert run("daily").returncode == 0
    files = next(line for line in _restic(calls()) if "--tag files" in line)
    assert f"{stack}/.env" in files
    assert f"{stack}/content/staff.json" in files
    assert "google_credentials.json" not in files  # empty on this server


def test_secrets_reach_restic_by_environment_never_on_the_command_line(server) -> None:
    _, _, run, calls = server
    assert run("daily").returncode == 0
    for line in _restic(calls()):
        assert not any(secret in line for secret in SECRETS)
    assert f"restic-env password={SECRETS[1]} aws={SECRETS[0]}" in calls()


def test_a_daily_run_forgets_old_snapshots_and_pings(server) -> None:
    _, _, run, calls = server
    assert run("daily").returncode == 0
    log = calls()
    forget = next(line for line in log if line.startswith("restic forget"))
    assert "--tag converge-agent" in forget
    assert "--keep-within 30d" in forget
    assert not any(line.startswith(("restic prune", "restic check")) for line in log)
    assert "curl https://hc-ping.test/key/converge-agent-backup/start?create=1" in log
    assert "curl https://hc-ping.test/key/converge-agent-backup?create=1" in log


def test_sunday_also_prunes_and_checks_a_sample_of_the_data(server) -> None:
    _, _, run, calls = server
    assert run("daily", weekday=7).returncode == 0
    commands = [line.split()[1] for line in _restic(calls())]
    assert commands[-3:] == ["forget", "prune", "check"]
    assert any("--read-data-subset" in line for line in calls())


def test_a_pre_deploy_snapshot_is_taken_without_touching_retention(server) -> None:
    _, _, run, calls = server
    assert run("pre-deploy-sha-abc", weekday=7).returncode == 0
    commands = [line.split()[1] for line in _restic(calls())]
    assert commands == ["backup", "backup"]
    assert any("--tag pre-deploy-sha-abc" in line for line in calls())


@pytest.mark.parametrize("fail", ["db", "forget"])
def test_any_failed_step_fails_the_run_and_is_reported(server, fail: str) -> None:
    _, _, run, calls = server
    result = run("daily", fail=fail)
    assert result.returncode != 0
    log = calls()
    assert "curl https://hc-ping.test/key/converge-agent-backup/fail?create=1" in log
    assert "curl https://hc-ping.test/key/converge-agent-backup?create=1" not in log
    if fail == "db":
        assert not any("--tag files" in line for line in log)


@pytest.mark.parametrize("missing", ["RESTIC_REPOSITORY", "RESTIC_PASSWORD", "AWS_SECRET_ACCESS_KEY"])
def test_missing_settings_stop_before_anything_runs(server, missing: str) -> None:
    _, config, run, calls = server
    config.write_text(
        "".join(line + "\n" for line in config.read_text().splitlines()
                if not line.startswith(missing + "="))
    )
    result = run("daily")
    assert result.returncode != 0
    assert missing in result.stderr
    assert _restic(calls()) == []


def test_a_label_must_be_a_plain_word(server) -> None:
    _, _, run, calls = server
    result = run("daily --tag x")
    assert result.returncode == 2
    assert calls() == []


@pytest.mark.parametrize("backup_fails", [False, True])
def test_a_release_backs_up_before_it_migrates(tmp_path: Path, backup_fails: bool) -> None:
    release = tmp_path / "release"
    release.mkdir()
    shutil.copy2(DEPLOY / "deploy.sh", release / "deploy.sh")
    (release / ".env").write_text("IMAGE_TAG=previous\n")
    (release / ".deployed-tag").write_text("previous\n")
    log = tmp_path / "calls.log"
    _executable(release / "backup.sh", (
        'echo "backup $*" >> "$CALLS"\n'
        f"exit {1 if backup_fails else 0}\n"
    ))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "docker", (
        'echo "docker $*" >> "$CALLS"\n'
        'if [ "$1" = inspect ]; then echo healthy; fi\n'
        'case "$*" in *pg_tables*) echo 0 ;; esac\n'
    ))

    result = subprocess.run(
        ["bash", str(release / "deploy.sh"), "new"],
        input="token", text=True, capture_output=True, check=False,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(log)},
    )

    lines = log.read_text().splitlines()
    backup = next(i for i, line in enumerate(lines) if line.startswith("backup "))
    assert lines[backup] == f"backup {release} pre-deploy-new"
    assert backup > lines.index("docker compose up -d --wait postgres redis")
    if backup_fails:
        assert result.returncode != 0
        assert not any("cli.migrate" in line for line in lines)
        assert "docker compose up -d reception-bot" not in lines
        assert (release / ".deployed-tag").read_text() == "previous\n"
    else:
        assert result.returncode == 0, result.stderr
        migrate = next(i for i, line in enumerate(lines) if "cli.migrate" in line)
        assert backup < migrate
