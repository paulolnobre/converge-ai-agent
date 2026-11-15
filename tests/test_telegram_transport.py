"""sendMessage when the network fails before Telegram answers."""

import logging

import httpx
import pytest

from core.integrations import telegram


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(telegram, "_retry_pause", instant)


def _serving(failures: list[Exception]):
    calls = []

    def serve(request):
        calls.append(request)
        if failures:
            raise failures.pop(0)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    return calls, serve


@pytest.mark.parametrize("send", ["send_message", "post_message"])
async def test_a_connection_that_never_opened_is_tried_once_more(monkeypatch, send):
    """Nothing reached Telegram, so a second try cannot send it twice."""
    calls, serve = _serving([httpx.ConnectError("refused")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        monkeypatch.setattr(telegram, "_http", lambda: client)
        result = await getattr(telegram, send)("1234", "oi")
    assert result and len(calls) == 2


@pytest.mark.parametrize("send", ["send_message", "post_message"])
async def test_a_timeout_after_sending_is_not_repeated(monkeypatch, send):
    """Telegram may have delivered it: repeating would send it twice."""
    calls, serve = _serving([httpx.ReadTimeout("slow")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        monkeypatch.setattr(telegram, "_http", lambda: client)
        result = await getattr(telegram, send)("1234", "oi")
    assert not result and len(calls) == 1


@pytest.mark.parametrize("send,where", [("send_message", "sendMessage"),
                                        ("post_message", "post_message")])
async def test_the_error_names_its_type_and_its_function(monkeypatch, caplog, send, where):
    """The admin alert shows the template only: the type must be in it."""
    _, serve = _serving([httpx.ReadTimeout("slow")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        monkeypatch.setattr(telegram, "_http", lambda: client)
        with caplog.at_level(logging.ERROR, logger=telegram.__name__):
            await getattr(telegram, send)("1234", "oi")
    record = caplog.records[-1]
    assert where in record.getMessage() and "ReadTimeout" in record.getMessage()
    assert record.exc_info and record.exc_info[0] is httpx.ReadTimeout
