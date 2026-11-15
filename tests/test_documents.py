"""What may leave the organization without a person saying so."""
import pytest

from core.config import settings
from core.integrations.workspace.models import FileRef
from core.services import documents


def _file(parent_id: str | None) -> FileRef:
    return FileRef(id="f1", name="proposta.pdf", parent_id=parent_id, provider="google")


def test_a_file_in_the_shareable_folder_may_be_sent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "shareable_folder_id", "public-folder")
    assert documents.is_shareable(_file("public-folder")) is True


def test_a_file_elsewhere_may_not(monkeypatch: pytest.MonkeyPatch):
    """A drive search for "contrato" finds other people's contracts too."""
    monkeypatch.setattr(settings, "shareable_folder_id", "public-folder")
    assert documents.is_shareable(_file("private-folder")) is False


def test_a_file_with_no_parent_may_not(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "shareable_folder_id", "public-folder")
    assert documents.is_shareable(_file(None)) is False


def test_nothing_is_shareable_when_no_folder_was_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    """Defaulting to yes here is how a private document gets e-mailed out."""
    monkeypatch.setattr(settings, "shareable_folder_id", None)
    assert documents.is_shareable(_file("any-folder")) is False


# ── A failure leaves no file name in the log or the audit ────────────────────

import logging  # noqa: E402

from core.integrations import workspace  # noqa: E402
from core.integrations.workspace.errors import WorkspaceError  # noqa: E402
from core.repositories import audit  # noqa: E402

_NAME = "Contrato — Maria Silva.pdf"


class _Failing:
    async def upload(self, *args, **kwargs):
        raise WorkspaceError("Google POST www.googleapis.com falhou (500)", status=500)

    async def download(self, *args, **kwargs):
        raise WorkspaceError("Google GET www.googleapis.com falhou (500)", status=500)


@pytest.fixture
def failing(monkeypatch: pytest.MonkeyPatch):
    rows: list[dict] = []

    async def record(conn, **kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(audit, "record", record)
    monkeypatch.setattr(workspace, "files", lambda: _Failing())
    monkeypatch.setattr(workspace, "configuration_report",
                        lambda: {"capabilities": {"files": "google", "mail": "google"}})
    return rows


async def test_a_failed_upload_logs_and_audits_no_file_name(failing, caplog):
    with caplog.at_level(logging.ERROR):
        assert await documents.file_incoming(None, name=_NAME, data=b"x",
                                             mime_type="application/pdf") is None
    assert "Maria" not in caplog.text
    assert failing[0]["target"] is None and "Maria" not in str(failing[0])
    assert failing[0]["detail"] == "WorkspaceError HTTP 500"


async def test_a_failed_send_audits_the_file_id_not_the_name(failing, caplog, monkeypatch):
    monkeypatch.setattr(settings, "shareable_folder_id", "public")
    file = FileRef(id="f9", name=_NAME, parent_id="public", provider="google")
    with caplog.at_level(logging.ERROR):
        assert await documents.send_file(None, file=file, to=["a@x.com"], subject="s",
                                         body="b") is False
    assert "Maria" not in caplog.text and "Maria" not in str(failing[0])
    assert failing[0]["target"] == "f9"


async def test_the_audit_names_the_drive_for_a_download_and_spacemail_for_the_send(
        failing, monkeypatch):
    monkeypatch.setattr(settings, "shareable_folder_id", "public")
    monkeypatch.setattr(settings, "spacemail_username", "team@getconverge.me")
    monkeypatch.setattr(settings, "spacemail_password", "x")
    monkeypatch.setattr(workspace, "configuration_report",
                        lambda: {"capabilities": {"files": "google", "mail": "google"}})
    file = FileRef(id="f9", name=_NAME, parent_id="public", provider="google")

    await documents.send_file(None, file=file, to=["a@x.com"], subject="s", body="b")
    assert failing[-1]["provider"] == "google"          # the drive failed

    class _Drive:
        async def download(self, *args, **kwargs):
            return b"%PDF"

    class _Refusing:
        async def send_message(self, *args, **kwargs):
            raise WorkspaceError("SMTP refused", status=554)

    monkeypatch.setattr(workspace, "files", lambda: _Drive())
    monkeypatch.setattr(workspace, "_spacemail", _Refusing())
    await documents.send_file(None, file=file, to=["a@x.com"], subject="s", body="b")
    assert failing[-1]["provider"] == "spacemail"       # the send failed
