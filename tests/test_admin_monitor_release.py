"""Run the documented release helper; no Docker daemon or server is contacted."""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize('services,expected', [('postgres\nreception-bot', False),
    ('postgres\nmonitor\nreception-bot', True), ('monitor-extra', False)])
def test_only_an_enabled_monitor_is_advanced_to_the_release(tmp_path, services, expected):
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\nif [ "$*" = "compose config --services" ]; then '
                      'printf "%s\\n" "$SERVICES"; else echo "$*" >> "$CALLS"; fi\n')
    docker.chmod(0o700)
    calls = tmp_path / 'calls'
    result = subprocess.run(['bash', str(ROOT/'deploy/admin-monitor.sh')], check=False,
        capture_output=True, text=True, env={**os.environ, 'PATH':str(tmp_path)+':'+os.environ['PATH'],
                                          'SERVICES':services, 'CALLS':str(calls)})
    assert result.returncode == 0, result.stderr
    assert calls.exists() is expected
    if expected:
        assert calls.read_text().splitlines() == ['compose up -d --no-deps monitor']


def test_ci_ships_the_optional_overlay_and_helper():
    ci = (ROOT/'.github/workflows/ci-cd.yml').read_text()
    assert 'deploy/admin-monitor.sh "$VPS:/opt/converge-agent/admin-monitor.sh"' in ci
    assert 'deploy/docker-compose.monitor.yml "$VPS:/opt/converge-agent/docker-compose.monitor.yml"' in ci
    deploy = (ROOT/'deploy/deploy.sh').read_text()
    assert deploy.index('bash ./admin-monitor.sh') > deploy.index('python -m cli.seal_data')
