"""Public installation assets never open authenticated data or arbitrary files."""
import struct

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.web import panel


@pytest.fixture
def app_client():
    app = FastAPI()
    panel.install(app)
    return TestClient(app, base_url="https://testserver")


def test_manifest_identity_and_launch_stay_inside_the_panel(app_client):
    response = app_client.get("/painel/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["id"] == manifest["scope"] == "/painel/"
    assert manifest["start_url"] == "/painel/#inbox"
    assert manifest["display"] == "standalone"
    assert len(manifest["icons"]) == 3


@pytest.mark.parametrize("name,size", [
    ("icon-192.png", 192), ("icon-512.png", 512),
    ("icon-maskable-512.png", 512), ("apple-touch-icon.png", 180),
])
def test_installation_icons_have_the_declared_pixel_dimensions(app_client, name, size):
    response = app_client.get("/painel/assets/" + name)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", response.content[16:24]) == (size, size)


def test_worker_revalidates_and_changes_with_the_release(app_client, monkeypatch):
    first = app_client.get("/painel/sw.js")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-cache"
    assert "__APP_VERSION__" not in first.text
    monkeypatch.setattr(settings, "app_version", 'test-release"quoted')
    second = app_client.get("/painel/sw.js")
    assert second.text != first.text
    # The environment-provided version is encoded as a JS string, not source.
    assert 'test-release\\"quoted-' in second.text
    assert "worker-src 'self'" in second.headers["content-security-policy"]
    assert "manifest-src 'self'" in second.headers["content-security-policy"]


def test_install_and_offline_pages_are_public_but_contain_no_personal_data(app_client):
    install = app_client.get("/painel/instalar")
    offline = app_client.get("/painel/offline")
    assert install.status_code == offline.status_code == 200
    assert "Adicionar à Tela de Início" in install.text
    assert "Sem conexão" in offline.text
    assert "/painel/api/" not in offline.text
    for path in ("/painel/assets/sw.js", "/painel/assets/index.html", "/painel/assets/manifest.webmanifest"):
        assert app_client.get(path).status_code == 404


def test_login_and_invite_share_the_installable_identity(app_client):
    for path in ("/painel/entrar", "/painel/convite"):
        html = app_client.get(path).text
        assert 'rel="manifest" href="/painel/manifest.webmanifest"' in html
        assert 'href="/painel/assets/apple-touch-icon.png"' in html
