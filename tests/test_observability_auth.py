"""Grafana sits behind the edge proxy, which asks the panel before each request
(deploy/edge/Caddyfile): a panel session with its second step, and a seat in
the admin room. Only then the proxy names the user for Grafana."""
import pytest

from core.repositories import admin_room
from tests.test_panel import _value, accounts, client, clock, signed_in  # noqa: F401

AUTH = "/painel/api/observability/auth"


@pytest.fixture
def members(monkeypatch: pytest.MonkeyPatch) -> set:
    seats: set = set()
    monkeypatch.setattr(admin_room, "is_member", lambda conn, user_id: _value(user_id in seats))
    return seats


def test_an_admin_is_let_in_and_named(signed_in, members):  # noqa: F811
    members.add(1)

    response = signed_in.get(AUTH)

    assert response.status_code == 200
    assert response.headers["x-converge-user"] == "painel-1"
    assert response.headers["cache-control"] == "no-store"


def test_a_signed_in_person_outside_the_admin_room_is_refused(signed_in, members):  # noqa: F811
    response = signed_in.get(AUTH)

    assert response.status_code == 403
    assert "x-converge-user" not in response.headers


def test_without_a_session_it_goes_to_sign_in(client, members):  # noqa: F811
    members.add(1)

    response = client.get(AUTH, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/painel/entrar"
    assert "x-converge-user" not in response.headers


def test_the_proxy_drops_a_login_header_sent_by_the_client():
    from pathlib import Path

    caddy = (Path(__file__).resolve().parent.parent / "deploy/edge/Caddyfile").read_text()
    block = caddy[caddy.index("handle /painel/grafana/*"):]
    rules = [line.strip() for line in block.splitlines() if not line.strip().startswith("#")]
    # Inside route, the written order is the order Caddy runs them in; outside
    # it, request_header would run after forward_auth and drop the real login.
    assert rules[1] == "route {", rules[:3]
    assert rules[2] == "request_header -X-Converge-User"
    assert rules[3].startswith("forward_auth ")
    assert "uri /painel/api/observability/auth" in block
    assert "handle @internal" in caddy, "/metrics and /ready stay inside the server"


def test_grafana_shares_no_network_with_the_bot_or_the_data():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    overlay = yaml.safe_load((root / "deploy/docker-compose.observability.yml").read_text())
    assert set(overlay["services"]["grafana"]["networks"]) == {"metrics", "grafana"}
    assert "internal" in overlay["services"]["prometheus"]["networks"]
    assert overlay["networks"]["grafana"] == {"name": "converge-grafana", "external": True}


def test_the_proxy_check_does_not_spend_a_device_renewal(signed_in, members, accounts,  # noqa: F811
                                                          monkeypatch):
    from core.repositories import panel_auth as auth_repo

    members.add(1)
    renewed: list = []

    async def renew(conn, token_hash):
        renewed.append(token_hash)
        return True

    monkeypatch.setattr(auth_repo, "renew_device_cookie", renew)
    for session in accounts.sessions.values():
        session["kind"] = "device"

    assert signed_in.get(AUTH).status_code == 200
    assert renewed == []


@pytest.mark.parametrize("services, expected", [
    ("postgres\nreception-bot", []),
    ("postgres\nprometheus\ngrafana\nnode-exporter", [
        "compose up -d --no-deps prometheus node-exporter grafana",
        "compose kill -s HUP prometheus"])])
def test_a_release_updates_observability_only_when_it_is_on(tmp_path, services, expected):
    import os
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\nif [ "$*" = "compose config --services" ]; then '
                      'printf "%s\\n" "$SERVICES"; else echo "$*" >> "$CALLS"; fi\n')
    docker.chmod(0o700)
    calls = tmp_path / "calls"
    result = subprocess.run(["bash", str(root / "deploy/observability.sh")], check=False,
                            capture_output=True, text=True,
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                                 "SERVICES": services, "CALLS": str(calls)})
    assert result.returncode == 0, result.stderr
    assert (calls.read_text().splitlines() if calls.exists() else []) == expected


def test_ci_ships_observability_and_the_release_runs_it():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    ci = (root / ".github/workflows/ci-cd.yml").read_text()
    assert 'deploy/observability/ "$VPS:/opt/converge-agent/observability/"' in ci
    assert 'deploy/observability.sh "$VPS:/opt/converge-agent/observability.sh"' in ci
    deploy = (root / "deploy/deploy.sh").read_text()
    assert deploy.index("bash ./observability.sh") > deploy.index("python -m cli.seal_data")
