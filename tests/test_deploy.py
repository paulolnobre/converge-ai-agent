"""Release script keeps the previous image when setup fails after startup."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "deploy.sh"


@pytest.mark.parametrize("failed_step", [
    "backup", "startup", "cli.load_content", "cli.set_webhook", "env_write", "tag_write", None,
])
def test_release_records_only_a_fully_configured_image(
    tmp_path: Path, failed_step: str | None
) -> None:
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    shutil.copy2(SCRIPT, release_dir / "deploy.sh")
    (release_dir / ".env").write_text(
        "PUBLIC_BASE_URL=https://example.test\nIMAGE_TAG=previous\n"
    )
    (release_dir / ".deployed-tag").write_text("previous\n")
    backup = release_dir / "backup.sh"
    backup.write_text(
        "#!/usr/bin/env bash\n"
        'echo "backup $*" >> "$DOCKER_LOG"\n'
        '[ "$FAILED_STEP" = backup ] && exit 1\n'
        'exit 0\n'
    )
    backup.chmod(0o755)

    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$IMAGE_TAG $*" >> "$DOCKER_LOG"\n'
        'vol=""; if [ -n "$MEDIA_SOURCE" ]; then vol=", \\"volumes\\": [{\\"type\\": \\"bind\\",'
        ' \\"source\\": \\"$MEDIA_SOURCE\\", \\"target\\": \\"/data/media\\"}]"; fi\n'
        'if [ "$1" = inspect ]; then echo healthy; fi\n'
        'case "$*" in *pg_tables*) echo 0 ;; esac\n'
        'if [ "$FAILED_STEP" = startup ] && [ "$IMAGE_TAG" = new ] '
        '&& [ "$*" = "compose up -d reception-bot" ]; then exit 1; fi\n'
        'if [ "$FAILED_STEP" != startup ] && [ -n "$FAILED_STEP" ] '
        '&& [[ "$*" == *"$FAILED_STEP"* ]]; then exit 1; fi\n'
    )
    docker.chmod(0o755)
    move = binaries / "mv"
    move.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$FAILED_STEP" = env_write ] && [ "${!#}" = .env ]; then exit 1; fi\n'
        'if [ "$FAILED_STEP" = tag_write ] && [ "${!#}" = .deployed-tag ]; then exit 1; fi\n'
        'exec /usr/bin/mv "$@"\n'
    )
    move.chmod(0o755)

    log = tmp_path / "docker.log"
    env = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
        "FAILED_STEP": failed_step or "",
    }
    result = subprocess.run(
        ["bash", str(release_dir / "deploy.sh"), "new"],
        input="test-token",
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    commands = log.read_text()
    if failed_step == "backup":
        # Stops before anything changed: no migration, no new bot, no rollback
        # needed because the previous one never stopped running.
        assert result.returncode != 0
        assert "Pre-deploy backup failed" in result.stdout
        assert "new compose up -d reception-bot" not in commands
        assert "cli.migrate" not in commands
        assert "previous compose up -d reception-bot" not in commands
        assert (release_dir / ".deployed-tag").read_text() == "previous\n"
        assert "IMAGE_TAG=previous\n" in (release_dir / ".env").read_text()
        return
    assert "new compose up -d reception-bot" in commands
    if failed_step:
        assert result.returncode != 0
        assert "previous compose up -d reception-bot" in commands
        assert (release_dir / ".deployed-tag").read_text() == "previous\n"
        assert "IMAGE_TAG=previous\n" in (release_dir / ".env").read_text()
    else:
        assert result.returncode == 0, result.stderr
        assert "previous compose up -d reception-bot" not in commands
        assert (release_dir / ".deployed-tag").read_text() == "new\n"
        assert "IMAGE_TAG=new\n" in (release_dir / ".env").read_text()


def test_a_release_keeps_compose_file_in_env(tmp_path: Path) -> None:
    """The encrypted-volume override is switched on by COMPOSE_FILE in .env; a
    release rewrites IMAGE_TAG there and must leave that line alone."""
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    shutil.copy2(SCRIPT, release_dir / "deploy.sh")
    compose_file = "COMPOSE_FILE=docker-compose.yml:docker-compose.encrypted.yml\n"
    (release_dir / ".env").write_text(
        "PUBLIC_BASE_URL=https://example.test\n" + compose_file + "IMAGE_TAG=previous\n"
    )
    (release_dir / ".deployed-tag").write_text("previous\n")
    backup = release_dir / "backup.sh"
    backup.write_text("#!/usr/bin/env bash\nexit 0\n")
    backup.chmod(0o755)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("#!/usr/bin/env bash\n"
                      'case "$*" in *pg_tables*) echo 0 ;; esac\n'
                      'if [ "$1" = inspect ]; then echo healthy; fi\nexit 0\n')
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}"}

    result = subprocess.run(["bash", str(release_dir / "deploy.sh"), "new"], input="test-token",
                            text=True, capture_output=True, env=env, check=False)

    assert result.returncode == 0, result.stderr
    written = (release_dir / ".env").read_text()
    assert compose_file in written and "IMAGE_TAG=new\n" in written



def test_a_last_line_without_a_newline_is_not_glued_to_image_tag(tmp_path: Path) -> None:
    """Someone adds a setting by hand and the file ends without a newline: the
    release must still write IMAGE_TAG on its own line. Glued, it broke two
    settings in production on 2026-10-29 (the reminder template's name and
    ASAAS_ENV), and the next start refused the configuration."""
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    shutil.copy2(SCRIPT, release_dir / "deploy.sh")
    (release_dir / ".env").write_text(
        "PUBLIC_BASE_URL=https://example.test\nIMAGE_TAG=previous\nASAAS_ENV=sandbox"
    )
    (release_dir / ".deployed-tag").write_text("previous\n")
    backup = release_dir / "backup.sh"
    backup.write_text("#!/usr/bin/env bash\nexit 0\n")
    backup.chmod(0o755)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("#!/usr/bin/env bash\n"
                      'case "$*" in *pg_tables*) echo 0 ;; esac\n'
                      'if [ "$1" = inspect ]; then echo healthy; fi\nexit 0\n')
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}"}

    result = subprocess.run(["bash", str(release_dir / "deploy.sh"), "new"], input="test-token",
                            text=True, capture_output=True, env=env, check=False)

    assert result.returncode == 0, result.stderr
    lines = (release_dir / ".env").read_text().splitlines()
    assert "ASAAS_ENV=sandbox" in lines and "IMAGE_TAG=new" in lines
    assert not any("IMAGE_TAG=" in line and not line.startswith("IMAGE_TAG=") for line in lines)

# ── The data format: an image never starts on data it cannot read ────────────

def _format_release(tmp_path: Path, **world: str) -> subprocess.CompletedProcess:
    """deploy.sh new, with the previous release "previous", against a fake
    docker that answers each image's format label, the database's and Redis's
    stored format, health and seal_data as `world` says."""
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    shutil.copy2(SCRIPT, release_dir / "deploy.sh")
    (release_dir / ".env").write_text("IMAGE_TAG=previous\n")
    (release_dir / ".deployed-tag").write_text("previous\n")
    backup = release_dir / "backup.sh"
    backup.write_text('#!/usr/bin/env bash\necho "backup" >> "$DOCKER_LOG"\n')
    backup.chmod(0o755)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$IMAGE_TAG $*" >> "$DOCKER_LOG"\n'
        'vol=""; if [ -n "$MEDIA_SOURCE" ]; then vol=", \\"volumes\\": [{\\"type\\": \\"bind\\",'
        ' \\"source\\": \\"$MEDIA_SOURCE\\", \\"target\\": \\"/data/media\\"}]"; fi\n'
        'case "$*" in\n'
        '  "compose config --format json") echo "{\\"services\\": {\\"redis\\": {\\"image\\": '
        '\\"redis:7\\"}, \\"reception-bot\\": {\\"image\\": \\"img:$IMAGE_TAG\\"$vol}}}" ;;\n'
        '  image\\ inspect*img:new) echo "$FORMAT_NEW" ;;\n'
        '  image\\ inspect*img:previous) echo "$FORMAT_PREVIOUS" ;;\n'
        '  *pg_tables*) [ -n "$PG_FAIL" ] && exit 2; echo "${PG_HAS_TABLE:-0}" ;;\n'
        '  *"FROM data_format"*) echo "${PG_FORMAT:-0}" ;;\n'
        '  *"redis-cli GET data_format"*) n=$(cat "$DOCKER_LOG.redis" 2>/dev/null || echo 0);'
        ' echo $((n + 1)) > "$DOCKER_LOG.redis";'
        ' if [ -n "$REDIS_FAIL_AFTER" ] && [ "$n" -ge "$REDIS_FAIL_AFTER" ]; then echo LOADING;'
        ' else echo "$REDIS_FORMAT"; fi ;;\n'
        '  *"cli.seal_data"*) exit "${SEAL_EXIT:-0}" ;;\n'
        'esac\n'
        'if [ "$1" = inspect ]; then\n'
        '  if [ -n "$NEW_UNHEALTHY" ] && [ "$IMAGE_TAG" = new ]; then echo unhealthy;'
        ' else echo healthy; fi\n'
        'fi\n'
        'exit 0\n'
    )
    docker.chmod(0o755)
    sleep = binaries / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)
    move = binaries / "mv"
    move.write_text(
        "#!/usr/bin/env bash\n"
        'if [ -n "$TAG_WRITE_FAILS" ] && [ "${!#}" = .deployed-tag ]; then exit 1; fi\n'
        'exec /usr/bin/mv "$@"\n'
    )
    move.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("")
    env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "DOCKER_LOG": str(log),
           "FORMAT_NEW": "1", "FORMAT_PREVIOUS": "", "PG_HAS_TABLE": "1", "PG_FORMAT": "",
           "REDIS_FORMAT": "", **world}
    result = subprocess.run(["bash", str(release_dir / "deploy.sh"), "new"], input="t",
                            text=True, capture_output=True, env=env, check=False)
    result.commands = log.read_text()  # type: ignore[attr-defined]
    result.tag = (release_dir / ".deployed-tag").read_text()  # type: ignore[attr-defined]
    result.env = (release_dir / ".env").read_text()  # type: ignore[attr-defined]
    return result


def test_a_new_image_below_the_stored_format_is_refused_before_any_change(tmp_path: Path):
    result = _format_release(tmp_path, FORMAT_NEW="", PG_FORMAT="1")
    assert result.returncode != 0
    assert "formato 0" in result.stdout and "formato 1" in result.stdout
    commands = result.commands  # type: ignore[attr-defined]
    assert "backup" not in commands and "cli.migrate" not in commands
    assert "compose up -d reception-bot" not in commands
    assert "compose stop reception-bot" not in commands  # the running bot is left alone


def test_the_redis_marker_alone_blocks_a_lower_image(tmp_path: Path):
    """Postgres restored from the backup (format 0), Redis still holding format 1."""
    result = _format_release(tmp_path, FORMAT_NEW="", PG_FORMAT="0", REDIS_FORMAT="1")
    assert result.returncode != 0 and "Redis" in result.stdout
    assert "cli.migrate" not in result.commands  # type: ignore[attr-defined]


def test_a_missing_data_format_table_counts_as_zero(tmp_path: Path):
    result = _format_release(tmp_path, PG_HAS_TABLE="0", PG_FORMAT="9")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.tag == "new\n"  # type: ignore[attr-defined]


def test_a_rollback_to_a_lower_format_stops_the_bot(tmp_path: Path):
    result = _format_release(tmp_path, NEW_UNHEALTHY="1", PG_FORMAT="1")
    assert result.returncode != 0
    commands = result.commands  # type: ignore[attr-defined]
    assert "compose stop reception-bot" in commands
    assert "previous compose up -d reception-bot" not in commands
    assert "não lê o formato" in result.stdout


def test_a_rollback_with_the_same_format_brings_back_the_previous_image(tmp_path: Path):
    result = _format_release(tmp_path, NEW_UNHEALTHY="1", FORMAT_PREVIOUS="1", PG_FORMAT="1")
    assert result.returncode != 0
    assert "previous compose up -d reception-bot" in result.commands  # type: ignore[attr-defined]


def test_seal_data_runs_after_the_tag_and_a_failure_does_not_roll_back(tmp_path: Path):
    result = _format_release(tmp_path, SEAL_EXIT="1")
    assert result.returncode != 0
    commands = result.commands  # type: ignore[attr-defined]
    assert "cli.seal_data" in commands
    assert result.tag == "new\n"  # type: ignore[attr-defined]
    assert "previous compose up -d reception-bot" not in commands
    assert "compose stop reception-bot" not in commands


def test_a_clean_release_seals_and_records_the_tag(tmp_path: Path):
    result = _format_release(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cli.seal_data" in result.commands  # type: ignore[attr-defined]


def test_the_image_declares_its_data_format():
    dockerfile = (SCRIPT.parent.parent / "Dockerfile").read_text()
    assert 'LABEL org.converge.data-format="2"' in dockerfile


def test_deploy_stops_without_the_media_folder_under_the_encrypted_overlay(tmp_path: Path):
    """With the encrypted volume locked, or the folder never created, the
    release stops with a clear message before anything changes."""
    result = _format_release(tmp_path, MEDIA_SOURCE=str(tmp_path / "srv" / "media"))
    assert result.returncode != 0
    assert "/data/media" in result.stdout and str(tmp_path / "srv" / "media") in result.stdout
    commands = result.commands  # type: ignore[attr-defined]
    assert "backup" not in commands and "cli.migrate" not in commands
    assert "compose up -d reception-bot" not in commands


def test_deploy_goes_on_with_the_media_folder_in_place(tmp_path: Path):
    (tmp_path / "srv" / "media").mkdir(parents=True)
    result = _format_release(tmp_path, MEDIA_SOURCE=str(tmp_path / "srv" / "media"))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("world", [{"PG_FAIL": "1"},
                                   {"REDIS_FORMAT": "LOADING Redis is loading the dataset"}])
def test_a_format_that_cannot_be_read_refuses_the_release(tmp_path: Path, world):
    """Fail closed: a Redis still loading its AOF, or a psql that failed, is not
    "format 0"."""
    result = _format_release(tmp_path, FORMAT_NEW="", **world)
    assert result.returncode != 0 and "não consegui ler" in result.stdout.lower()
    assert "cli.migrate" not in result.commands  # type: ignore[attr-defined]


def test_a_rollback_that_cannot_read_the_format_stops_the_bot(tmp_path: Path):
    """Readable at the start, unreadable when the rollback asks: stop, do not guess."""
    result = _format_release(tmp_path, NEW_UNHEALTHY="1", FORMAT_PREVIOUS="1",
                             REDIS_FAIL_AFTER="1")
    assert result.returncode != 0
    commands = result.commands  # type: ignore[attr-defined]
    assert "compose stop reception-bot" in commands
    assert "previous compose up -d reception-bot" not in commands


def test_a_refused_rollback_leaves_the_new_tag_for_the_next_boot(tmp_path: Path):
    """The unlock script runs `compose up -d` at boot with .env's IMAGE_TAG: it
    must not be the image that cannot read the data."""
    result = _format_release(tmp_path, NEW_UNHEALTHY="1", PG_FORMAT="1")
    assert result.returncode != 0
    assert "IMAGE_TAG=new\n" in result.env  # type: ignore[attr-defined]
    assert result.tag == "previous\n"  # type: ignore[attr-defined]


def test_a_failed_tag_write_with_a_refused_rollback_leaves_the_new_image_in_env(tmp_path: Path):
    """.env was already moved, the tag write fails, .env goes back to the
    previous image — which cannot read the data: the next boot must not start it."""
    result = _format_release(tmp_path, TAG_WRITE_FAILS="1", PG_FORMAT="1")
    assert result.returncode != 0
    assert "compose stop reception-bot" in result.commands  # type: ignore[attr-defined]
    assert "IMAGE_TAG=new\n" in result.env  # type: ignore[attr-defined]
