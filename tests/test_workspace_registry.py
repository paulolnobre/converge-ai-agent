"""Which suite answers which capability, and what /ready reports."""
import pytest

from core.config import settings
from core.integrations import workspace
from core.integrations.workspace.errors import WorkspaceNotConfigured
from core.integrations.workspace.google import GoogleWorkspace
from core.integrations.workspace.microsoft import MicrosoftWorkspace


def test_every_capability_follows_the_default_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "workspace_provider", "microsoft")
    for capability in (workspace.calendar, workspace.mail, workspace.files, workspace.sheets):
        assert isinstance(capability(), MicrosoftWorkspace)


def test_a_split_tenant_can_override_one_capability(monkeypatch: pytest.MonkeyPatch):
    """Mail on Microsoft 365, the shared drive on Google."""
    monkeypatch.setattr(settings, "workspace_provider", "microsoft")
    monkeypatch.setattr(settings, "files_provider", "google")
    assert isinstance(workspace.mail(), MicrosoftWorkspace)
    assert isinstance(workspace.files(), GoogleWorkspace)


def test_an_unknown_provider_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "workspace_provider", "dropbox")
    with pytest.raises(WorkspaceNotConfigured):
        workspace.files()


def test_the_report_names_the_provider_missing_its_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "workspace_provider", "microsoft")
    monkeypatch.setattr(settings, "ms_tenant_id", None)
    report = workspace.configuration_report()
    assert report["status"] == "incomplete"
    assert report["missing"] == ["microsoft"]


def test_the_report_never_echoes_a_credential(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ms_client_secret", "super-secret-value")
    monkeypatch.setattr(settings, "google_refresh_token", "1//another-secret")
    assert "super-secret-value" not in str(workspace.configuration_report())
    assert "another-secret" not in str(workspace.configuration_report())
