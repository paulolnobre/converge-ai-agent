"""Telling the team about a person's e-mail: what counts as automatic, what the
alert says, the order of save-then-alert, and the IMAP conversation."""
import asyncio
import contextlib
import imaplib

import pytest

from core.config import settings
from core.integrations.workspace import WorkspaceError
from core.integrations.workspace import spacemail_imap as imap
from core.integrations.workspace.errors import WorkspaceAuthError, WorkspaceTransient
from core.models import Contact
from core.services import mail_alerts


def _raw(body="Olá, tudo bem?", subject="Orçamento", sender="Ana Souza <ana@example.com>",
         extra="") -> bytes:
    return (f"From: {sender}\r\nTo: team@getconverge.me\r\nSubject: {subject}\r\n"
            f"{extra}MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}\r\n").encode()


# ── What is automatic ──────────────────────────────────────────────────────

@pytest.mark.parametrize("extra", [
    "List-Unsubscribe: <mailto:u@x.com>\r\n", "Precedence: bulk\r\n", "Precedence: list\r\n",
    "Auto-Submitted: auto-generated\r\n", "X-Auto-Response-Suppress: All\r\n"])
def test_automatic_headers_are_not_a_person(extra):
    assert imap.parse(1, _raw(extra=extra)).automated is True


@pytest.mark.parametrize("sender", [
    "no-reply@x.com", "noreply@x.com", "NoReply+abc@x.com", "donotreply@x.com",
    "mailer-daemon@x.com", "postmaster@x.com", "notifications@github.com"])
def test_automatic_senders_are_not_a_person(sender):
    assert imap.parse(1, _raw(sender=sender)).automated is True


def test_a_person_passes_even_with_auto_submitted_no():
    mail = imap.parse(1, _raw(extra="Auto-Submitted: no\r\nPrecedence: first-class\r\n"))
    assert mail.automated is False


def test_the_parts_are_read_with_accents_and_one_line_each():
    mail = imap.parse(7, _raw(subject="=?utf-8?q?Or=C3=A7amento_novo?=",
                              body="Linha 1\r\n\r\nLinha 2\x07 " + "x" * 400))
    assert mail.uid == 7 and mail.sender_email == "ana@example.com"
    assert mail.sender_name == "Ana Souza" and mail.subject == "Orçamento novo"
    assert "\n" not in mail.preview and "\x07" not in mail.preview
    assert mail.preview.startswith("Linha 1 Linha 2") and len(mail.preview) == 200


def test_html_only_mail_loses_its_markup():
    raw = ("From: a@b.com\r\nSubject: oi\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
           "<style>p{x:y}</style><p>Quero <b>marcar</b> uma conversa &amp; saber o valor</p>"
           ).encode()
    assert imap.parse(1, raw).preview == "Quero  marcar  uma conversa & saber o valor"


# ── The IMAP conversation ──────────────────────────────────────────────────

class FakeIMAP:
    log: list = []
    inbox: dict = {}
    validity = b"77"
    login_error: Exception | None = None
    broken: set = set()

    def __init__(self, host, port, ssl_context=None, timeout=None):
        FakeIMAP.log.append(("connect", host, port, ssl_context is not None))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if FakeIMAP.login_error:
            raise FakeIMAP.login_error
        FakeIMAP.log.append(("login", user))

    def select(self, folder, readonly=False):
        FakeIMAP.log.append(("select", folder, readonly))

    def response(self, name):
        return name, [FakeIMAP.validity]

    def uid(self, command, *args):
        FakeIMAP.log.append((command, *args))
        if command == "SEARCH":
            criteria = args[0]
            low = 1 if criteria == "ALL" else int(criteria.split()[1].split(":")[0])
            found = [u for u in FakeIMAP.inbox if u >= low] or ([max(FakeIMAP.inbox)]
                                                                 if FakeIMAP.inbox else [])
            return "OK", [" ".join(str(u) for u in found).encode()]
        if int(args[0]) in FakeIMAP.broken:
            return "NO", [None]
        return "OK", [(b"1 (BODY[] {9}", FakeIMAP.inbox[int(args[0])]), b")"]


@pytest.fixture
def fake_imap(monkeypatch):
    monkeypatch.setattr(settings, "spacemail_username", "team@getconverge.me")
    monkeypatch.setattr(settings, "spacemail_password", "pw")
    monkeypatch.setattr(settings, "spacemail_imap_host", "imap.test")
    FakeIMAP.log, FakeIMAP.login_error, FakeIMAP.broken = [], None, set()
    FakeIMAP.inbox = {1: _raw(subject="velho"), 2: _raw(subject="dois"), 3: _raw(subject="três")}
    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


def test_the_first_look_only_says_where_the_mailbox_stands(fake_imap):
    result = imap.fetch_since(None)
    assert (result.uidvalidity, result.max_uid, result.mails) == (77, 3, [])
    assert not any(c[0] == "FETCH" for c in fake_imap.log)


def test_it_opens_read_only_and_only_peeks(fake_imap):
    result = imap.fetch_since((77, 1))
    assert [m.subject for m in result.mails] == ["dois", "três"] and result.max_uid == 3
    assert ("select", "INBOX", True) in fake_imap.log
    fetches = [c for c in fake_imap.log if c[0] == "FETCH"]
    assert fetches and all("BODY.PEEK" in c[2] for c in fetches)
    assert fake_imap.log[0] == ("connect", "imap.test", 993, True)


def test_a_renumbered_mailbox_is_only_looked_at_not_read(fake_imap):
    result = imap.fetch_since((50, 400))
    assert (result.uidvalidity, result.max_uid, result.mails) == (77, 3, [])
    assert not any(c[0] == "FETCH" for c in fake_imap.log)


def test_a_message_the_server_will_not_give_is_skipped_not_blocking(fake_imap):
    fake_imap.broken = {2}
    result = imap.fetch_since((77, 1))
    assert [m.subject for m in result.mails] == ["três"]
    assert result.skipped == 1 and result.max_uid == 3


def test_more_than_the_cap_counts_what_was_left_out(fake_imap, monkeypatch):
    monkeypatch.setattr(imap, "_MAX_PER_PASS", 2)
    fake_imap.inbox = {n: _raw(subject=f"m{n}") for n in range(1, 8)}
    result = imap.fetch_since((77, 0))
    assert [m.uid for m in result.mails] == [6, 7]
    assert result.skipped == 5 and result.max_uid == 7


def test_a_slow_read_stops_and_leaves_the_rest_for_the_next_pass(fake_imap, monkeypatch):
    clock = iter([0, 1, 100, 100, 100])
    monkeypatch.setattr(imap.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(imap, "_READ_BUDGET_SECONDS", 10)
    result = imap.fetch_since((77, 1))
    assert [m.uid for m in result.mails] == [2]
    assert result.max_uid == 2 and result.skipped == 0


def test_nothing_new_keeps_the_point_where_it_was(fake_imap):
    result = imap.fetch_since((77, 3))
    assert result.mails == [] and result.max_uid == 3


def test_failures_are_workspace_errors_without_addresses(fake_imap):
    fake_imap.login_error = imaplib.IMAP4.error("[AUTHENTICATIONFAILED] team@getconverge.me")
    with pytest.raises(WorkspaceAuthError) as caught:
        imap.fetch_since((77, 1))
    assert "getconverge" not in str(caught.value) and caught.value.__suppress_context__
    fake_imap.login_error = OSError("unreachable team@getconverge.me")
    with pytest.raises(WorkspaceTransient) as caught:
        imap.fetch_since((77, 1))
    assert "getconverge" not in str(caught.value)


# ── The pass: save first, then alert ───────────────────────────────────────

class _Pool:
    def __init__(self, events):
        self.events = events

    def connection(self):
        events = self.events

        class Conn:
            async def commit(self):
                events.append(("commit",))

        @contextlib.asynccontextmanager
        async def ctx():
            yield Conn()
        return ctx()


@pytest.fixture
def world(monkeypatch):
    state: dict = {"saved": None, "events": [], "contacts": {}, "locked": True}
    monkeypatch.setattr(settings, "spacemail_username", "team@getconverge.me")
    monkeypatch.setattr(settings, "spacemail_password", "pw")
    monkeypatch.setattr(settings, "spacemail_alerts", True)

    async def get_pool():
        return _Pool(state["events"])

    class _Own:
        async def commit(self):
            state["events"].append(("commit",))

        async def close(self):
            state["events"].append(("closed",))

    async def connect_async():
        return _Own()

    async def try_lock(conn, mailbox):
        return state["locked"]

    async def get_state(conn, mailbox):
        return state["saved"]

    async def set_state(conn, mailbox, validity, last):
        state["saved"] = (validity, last)
        state["events"].append(("saved", last))

    async def find_by_email(conn, email):
        return state["contacts"].get(email)

    async def notify_team(text, contact_id):
        state["events"].append(("telegram", text, contact_id))
        return True

    def push_to_team(text, contact):
        state["events"].append(("push-known", text, contact.id))

    def schedule_team(**kwargs):
        state["events"].append(("push-unknown", kwargs["title"], kwargs["url"]))

    monkeypatch.setattr(mail_alerts, "get_pool", get_pool)
    monkeypatch.setattr(mail_alerts, "connect_async", connect_async)
    monkeypatch.setattr(mail_alerts.repo, "try_lock", try_lock)
    monkeypatch.setattr(mail_alerts.repo, "get_state", get_state)
    monkeypatch.setattr(mail_alerts.repo, "set_state", set_state)
    monkeypatch.setattr(mail_alerts.contacts_repo, "find_by_email", find_by_email)
    monkeypatch.setattr(mail_alerts.inbox, "notify_team", notify_team)
    monkeypatch.setattr(mail_alerts.inbox, "push_to_team", push_to_team)
    monkeypatch.setattr(mail_alerts.push, "schedule_team", schedule_team)
    state["fetch"] = lambda st: imap.Fetched(77, 0, [])
    monkeypatch.setattr(mail_alerts.imap, "fetch_since", lambda st: state["fetch"](st))
    return state


def _mail(uid, automated=False, email="ana@example.com", subject="Orçamento"):
    return imap.IncomingMail(uid, "Ana Souza", email, subject, "Olá, tudo bem?", automated)


async def test_the_first_pass_marks_the_point_and_alerts_nothing(world):
    world["fetch"] = lambda st: imap.Fetched(77, 26, [])
    assert await mail_alerts.run_pass() == 0
    assert world["saved"] == (77, 26)
    assert [e[0] for e in world["events"]] == ["saved", "commit", "closed"]


async def test_the_point_is_saved_before_any_alert(world):
    world["saved"] = (77, 3)
    world["fetch"] = lambda st: imap.Fetched(77, 5, [_mail(4), _mail(5, automated=True)])
    assert await mail_alerts.run_pass() == 1
    kinds = [e[0] for e in world["events"]]
    assert kinds[:2] == ["saved", "commit"] and world["saved"] == (77, 5)
    assert kinds.count("telegram") == 1 and kinds.index("commit") < kinds.index("telegram")
    assert any("Ana Souza" in e[1] for e in world["events"] if e[0] == "telegram")


async def test_an_unknown_sender_opens_the_panel_and_a_known_one_the_conversation(world):
    world["saved"] = (77, 1)
    world["contacts"]["ana@example.com"] = Contact(id=9, chat_id="wa:5591")
    world["fetch"] = lambda st: imap.Fetched(
        77, 3, [_mail(2), _mail(3, email="novo@example.com")])
    assert await mail_alerts.run_pass() == 2
    events = world["events"]
    assert any(e[0] == "push-known" and e[2] == 9 for e in events)
    assert any(e[0] == "telegram" and e[2] == 9 for e in events)
    assert any(e[0] == "push-unknown" and e[2] == "/painel/#inbox" for e in events)
    assert any(e[0] == "telegram" and e[2] is None for e in events)


async def test_an_e_mail_from_a_test_contact_is_not_announced_in_the_group(world):
    world["saved"] = (77, 1)
    world["contacts"]["ana@example.com"] = Contact(id=9, chat_id="wa:5591", test_mode="novo")
    world["fetch"] = lambda st: imap.Fetched(77, 2, [_mail(2)])
    await mail_alerts.run_pass()
    events = world["events"]
    assert any(e[0] == "push-known" and e[2] == 9 for e in events)
    assert not any(e[0] == "telegram" for e in events)


async def test_a_renumbered_mailbox_saves_the_new_point(world):
    world["saved"] = (50, 400)
    world["fetch"] = lambda st: imap.Fetched(77, 12, [])
    assert await mail_alerts.run_pass() == 0 and world["saved"] == (77, 12)


async def test_another_replica_holding_the_lock_means_no_read_at_all(world):
    world["locked"] = False
    world["fetch"] = lambda st: pytest.fail("must not read")
    assert await mail_alerts.run_pass() == 0 and world["events"] == [("closed",)]


async def test_what_was_left_out_of_the_read_is_counted_in_the_last_line(world):
    world["saved"] = (77, 0)
    world["fetch"] = lambda st: imap.Fetched(77, 9, [_mail(9)], skipped=7)
    await mail_alerts.run_pass()
    assert "Mais 7 e-mails novos" in [e for e in world["events"] if e[0] == "telegram"][-1][1]


async def test_more_than_ten_become_ten_alerts_and_one_line(world):
    world["saved"] = (77, 0)
    world["fetch"] = lambda st: imap.Fetched(77, 14, [_mail(n) for n in range(1, 15)])
    assert await mail_alerts.run_pass() == 10
    lines = [e[1] for e in world["events"] if e[0] == "telegram"]
    assert len(lines) == 11 and "Mais 4 e-mails novos" in lines[-1]


async def test_a_failing_alert_does_not_stop_the_others(world, monkeypatch):
    world["saved"] = (77, 0)
    world["fetch"] = lambda st: imap.Fetched(77, 2, [_mail(1), _mail(2)])
    calls = []

    async def flaky(conn, mail):
        calls.append(mail.uid)
        if mail.uid == 1:
            raise RuntimeError("telegram down")
        return True

    monkeypatch.setattr(mail_alerts, "_alert", flaky)
    assert await mail_alerts.run_pass() == 1    # only the one that went out
    assert calls == [1, 2]


@pytest.mark.parametrize(("alerts", "user"), [(False, "team@getconverge.me"), (True, "")])
async def test_it_idles_when_off_or_without_the_login(world, monkeypatch, alerts, user):
    monkeypatch.setattr(settings, "spacemail_alerts", alerts)
    monkeypatch.setattr(settings, "spacemail_username", user)
    world["fetch"] = lambda st: pytest.fail("must not read")
    assert await mail_alerts.run_pass() == 0


async def test_the_worker_survives_a_failure_and_logs_the_outage_once(world, caplog, monkeypatch):
    monkeypatch.setattr(settings, "email_alert_minutes", 1)
    stop = asyncio.Event()
    runs = []

    def boom(st):
        runs.append(1)
        if len(runs) >= 3:
            stop.set()
        raise WorkspaceTransient("IMAP imap.test: connection or reply failed.")

    world["fetch"] = boom
    monkeypatch.setattr(asyncio, "wait_for", _instant_wait)
    with caplog.at_level("WARNING"):
        await mail_alerts.run_mail_alert_worker(stop)
    assert len(runs) == 3
    assert caplog.text.count("could not read") == 1 and "example.com" not in caplog.text


async def _instant_wait(awaitable, timeout):
    awaitable.close()
    await asyncio.sleep(0)
    raise TimeoutError


def test_workspace_error_is_what_the_worker_expects():
    assert issubclass(WorkspaceAuthError, WorkspaceError)


async def test_an_e_mail_from_a_blocked_contact_tells_nobody(world):
    from datetime import UTC, datetime

    world["saved"] = (77, 1)
    world["contacts"]["ana@example.com"] = Contact(id=9, chat_id="wa:5591",
                                                   blocked_at=datetime.now(UTC))
    world["fetch"] = lambda st: imap.Fetched(77, 2, [_mail(2)])
    assert await mail_alerts.run_pass() == 0
    assert not any(e[0] in ("push-known", "push-unknown", "telegram") for e in world["events"])
