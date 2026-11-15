"""Test-wide isolation.

No test in this suite reaches Postgres, Redis, Telegram or a workspace API. The
fixtures below make that structural rather than a convention people remember.
"""
import base64

import pytest

from core.config import settings
from core.integrations import payments, workspace


@pytest.fixture(autouse=True)
def isolate_workspace_clients():
    """A provider chosen by one test must not leak into the next."""
    workspace.reset()
    payments.reset()
    yield
    workspace.reset()
    payments.reset()


@pytest.fixture(autouse=True)
def isolate_forms():
    """The Flows are read once and cached: a test that points them elsewhere,
    or hides their ids, must not hand that to the next test."""
    from core.services import forms

    forms.reset()
    yield
    forms.reset()


@pytest.fixture(autouse=True)
def pin_office_hours(monkeypatch: pytest.MonkeyPatch):
    """Whether the desk keeps real office hours must not depend on whatever a
    developer happens to have in their own .env — a test that wants
    DESK_ALWAYS_OPEN=True says so itself."""
    monkeypatch.setattr(settings, "desk_always_open", False)
    # Same for the team group: a test that hands off says which group.
    monkeypatch.setattr(settings, "team_chat_id", "")
    # And for video meetings: a test about the Meet link turns them on itself.
    monkeypatch.setattr(settings, "online_meetings", False)
    # And for the panel's address: a test about the "Abrir no painel" button
    # sets it itself.
    monkeypatch.setattr(settings, "public_base_url", "")


@pytest.fixture(autouse=True)
def isolate_rate_limiter():
    """The in-process fallback limiter in core.handlers.message is a
    module-level singleton, keyed by chat_id and clocked by time.monotonic —
    without this, hits from an earlier test in the same session count against
    a later one that reuses the same chat_id, and enough tests in one file
    trips it for real."""
    from core.handlers import message

    message._limiter._events.clear()
    message._limiter._notified.clear()
    yield


@pytest.fixture(autouse=True)
def data_keys(monkeypatch: pytest.MonkeyPatch):
    """Fixed keys for every test, so sealing works without a developer's .env
    and no test depends on another's rotation."""
    from core import crypto

    monkeypatch.setattr(settings, "data_keys", "1:" + _TEST_KEY)
    monkeypatch.setattr(settings, "data_index_key", _TEST_INDEX_KEY)
    crypto.reset_keys()
    yield
    crypto.reset_keys()


# Built, not written: obviously test values, and nothing a scanner mistakes
# for a leaked key.
_TEST_KEY = base64.b64encode(bytes(range(32))).decode()
_TEST_INDEX_KEY = base64.b64encode(bytes(range(32, 64))).decode()


@pytest.fixture
def offline_cache(monkeypatch: pytest.MonkeyPatch):
    """Force the in-process fallback path, as if Redis were unreachable."""
    from core.integrations import cache

    monkeypatch.setattr(cache, "available", lambda: False)
    return cache


@pytest.fixture(autouse=True)
def nobody_blocked(request, monkeypatch: pytest.MonkeyPatch):
    """Most tests drive the handler over a fake connection: nobody is blocked
    there. The Postgres tests (*_pg) ask the real table."""
    if request.module.__name__.endswith("_pg"):
        return
    from core.repositories import contacts as contacts_repo

    async def not_blocked(conn, chat_id):
        return False

    async def not_blocked_now(chat_id):
        return False

    monkeypatch.setattr(contacts_repo, "is_blocked", not_blocked)
    monkeypatch.setattr(contacts_repo, "is_blocked_now", not_blocked_now)
