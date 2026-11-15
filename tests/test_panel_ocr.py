"""Manual OCR respects login, original-file expiry and a bounded local worker."""
# ruff: noqa: F811
import asyncio
from pathlib import Path

import pytest

from core.config import settings
from core.services import ocr
from core.web import panel_ocr
from tests.test_panel import accounts, client, clock, signed_in  # noqa: F401
from tests.test_panel_media import PNG, files  # noqa: F401

PATH = "/painel/api/inbox/7/media/1/ocr"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "inbox_ocr_enabled", True)


def test_ocr_requires_mfa_session_and_same_origin(client, signed_in):
    assert client.post(PATH, headers={"Origin": "https://other.test"}).status_code == 403
    client.cookies.clear()
    assert client.post(PATH).status_code == 401


def test_disabled_feature_refuses_even_a_signed_in_user(signed_in, monkeypatch):
    monkeypatch.setattr(settings, "inbox_ocr_enabled", False)
    assert signed_in.post(PATH).status_code == 404


def test_reads_only_the_contacts_available_file_and_never_caches_text(signed_in, enabled, files,
                                                                    monkeypatch):
    files(1, PNG)
    received = []

    async def extract(data):
        received.append(data)
        return {"text": "<script>texto de teste</script>", "pages": 1, "limited": False}

    monkeypatch.setattr(ocr, "extract", extract)
    response = signed_in.post(PATH)
    assert response.status_code == 200
    assert received == [PNG]
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["text"] == "<script>texto de teste</script>"
    assert signed_in.post(PATH.replace("/7/", "/8/")).status_code == 404
    assert received == [PNG]


def test_expiry_while_extracting_discards_the_result(signed_in, enabled, files, monkeypatch):
    files(1, PNG)

    async def expired(conn, contact_id, media_id):
        return None

    async def extract(data):
        monkeypatch.setattr(panel_ocr.media_repo, "servable", expired)
        return {"text": "private result", "pages": 1, "limited": False}

    monkeypatch.setattr(ocr, "extract", extract)
    response = signed_in.post(PATH)
    assert response.status_code == 404
    assert "private result" not in response.text


@pytest.mark.parametrize("kind,data", [("audio", b"OggSvoice"),
                                       ("document", b"<html>not an image</html>")])
def test_audio_and_disguised_documents_are_not_sent_to_ocr(signed_in, enabled, files, kind, data):
    files(1, data, kind=kind)
    assert signed_in.post(PATH).status_code == 415


def test_a_missing_original_is_not_extracted(signed_in, enabled, files):
    files(1, PNG, write=False)
    assert signed_in.post(PATH).status_code == 404
    assert files.missing == [1]


async def test_no_queue_of_ocr_jobs_is_created():
    async with panel_ocr.slot():
        with pytest.raises(ocr.OcrError) as refused:
            async with panel_ocr.slot():
                pytest.fail("A second job must not start")
    assert refused.value.status == 429
    async with panel_ocr.slot():
        pass  # the slot is usable again


async def test_oversized_input_never_starts_a_child():
    with pytest.raises(ocr.OcrError) as refused:
        await ocr.extract(b"x" * (ocr.MAX_INPUT + 1))
    assert refused.value.status == 413


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancellation_kills_children_and_removes_ram_files(monkeypatch, cancel):
    folders, killed, environments = [], [], []
    started = asyncio.Event()

    class Process:
        pid = 123456789

        async def communicate(self, data):
            (folders[0] / "source.pdf").write_bytes(data)
            started.set()
            await asyncio.Event().wait()

        async def wait(self):
            return -9

    async def spawn(*args, **kwargs):
        folders.append(Path(args[-1]))
        environments.append(kwargs["env"])
        return Process()

    monkeypatch.setattr(ocr.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(ocr.os, "killpg", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(ocr, "WALL_SECONDS", .02 if not cancel else 5)
    task = asyncio.create_task(ocr.extract(b"synthetic private file"))
    await started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ocr.OcrError) as refused:
            await task
        assert refused.value.status == 422
    assert killed == [Process.pid]
    assert not folders[0].exists()
    assert set(environments[0]) == {"PATH", "LANG", "OMP_THREAD_LIMIT"}
