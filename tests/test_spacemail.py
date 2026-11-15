"""Sending through Spacemail: the message, the SMTP conversation, the failures
(which never carry an address), and who sends when."""
import smtplib

import pytest

from core.config import settings
from core.integrations import workspace
from core.integrations.workspace import OutgoingMail, WorkspaceError, spacemail
from core.integrations.workspace.errors import (
    WorkspaceAuthError,
    WorkspaceNotConfigured,
    WorkspaceTransient,
)
from core.integrations.workspace.models import Attachment

SENDER = "team@getconverge.me"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "spacemail_username", SENDER)
    monkeypatch.setattr(settings, "spacemail_password", "s3cret-pass")
    monkeypatch.setattr(settings, "spacemail_host", "smtp.test")
    monkeypatch.setattr(settings, "spacemail_port", 465)


class FakeSMTP:
    calls: list = []
    fail_with: Exception | None = None
    fail_on: str = "send"

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.calls.append(("connect", host, port, context is not None))
        if FakeSMTP.fail_with and FakeSMTP.fail_on == "connect":
            raise FakeSMTP.fail_with

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        FakeSMTP.calls.append(("login", user, password))
        if FakeSMTP.fail_with and FakeSMTP.fail_on == "login":
            raise FakeSMTP.fail_with

    refused: dict = {}

    def send_message(self, mail, to_addrs=None):
        FakeSMTP.calls.append(("send", mail, to_addrs))
        if FakeSMTP.fail_with and FakeSMTP.fail_on == "send":
            raise FakeSMTP.fail_with
        return FakeSMTP.refused


@pytest.fixture
def smtp(monkeypatch, configured):
    FakeSMTP.calls, FakeSMTP.fail_with, FakeSMTP.fail_on = [], None, "send"
    FakeSMTP.refused = {}
    monkeypatch.setattr(spacemail.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def _mail(**extra) -> OutgoingMail:
    return OutgoingMail(to=["ana@example.com"], subject="Lembrete: reunião amanhã",
                        body_text="Olá, Ana! Confirmamos às 14h.", **extra)


def test_the_message_is_from_the_mailbox_with_utf8_and_without_bcc():
    mail = spacemail.build(_mail(cc=["cc@example.com"], bcc=["oculto@example.com"]), SENDER)
    assert mail["From"] == SENDER and mail["To"] == "ana@example.com"
    assert mail["Cc"] == "cc@example.com" and mail["Bcc"] is None
    assert mail["Subject"] == "Lembrete: reunião amanhã"
    assert "Confirmamos às 14h." in mail.get_content()
    assert mail["Message-ID"].endswith("@getconverge.me>")


def test_an_attachment_travels_with_its_name_and_type():
    pdf = Attachment(filename="proposta.pdf", content_type="application/pdf", data=b"%PDF-1")
    parts = list(spacemail.build(_mail(attachments=[pdf]), SENDER).iter_attachments())
    assert [(p.get_filename(), p.get_content_type()) for p in parts] == [
        ("proposta.pdf", "application/pdf")]


async def test_it_logs_in_over_ssl_and_sends_to_everyone_in_the_envelope(smtp):
    sent_id = await spacemail.SpacemailMailer().send_message(
        "ignored@example.com", _mail(cc=["cc@example.com"], bcc=["oculto@example.com"]))
    kinds = [c[0] for c in smtp.calls]
    assert kinds == ["connect", "login", "send"]
    assert smtp.calls[0][1:] == ("smtp.test", 465, True)
    assert smtp.calls[1][1:] == (SENDER, "s3cret-pass")
    assert smtp.calls[2][2] == ["ana@example.com", "cc@example.com", "oculto@example.com"]
    assert sent_id.startswith("<") and smtp.calls[2][1]["From"] == SENDER


@pytest.mark.parametrize(("error", "kind", "status"), [
    (smtplib.SMTPAuthenticationError(535, b"bad creds for team@getconverge.me"),
     WorkspaceAuthError, 535),
    (smtplib.SMTPRecipientsRefused({"ana@example.com": (550, b"no")}), WorkspaceError, None),
    (smtplib.SMTPDataError(554, b"rejected ana@example.com"), WorkspaceError, 554),
    (smtplib.SMTPServerDisconnected("gone"), WorkspaceTransient, None),
    (TimeoutError("slow"), WorkspaceTransient, None),
])
async def test_failures_are_workspace_errors_without_addresses_or_password(smtp, error, kind, status):
    smtp.fail_with = error
    with pytest.raises(kind) as caught:
        await spacemail.SpacemailMailer().send_message("x", _mail())
    text = str(caught.value) + repr(caught.value.__cause__)
    assert caught.value.status == status
    assert "example.com" not in text and "getconverge.me" not in text and "s3cret" not in text
    assert caught.value.__suppress_context__


async def test_a_connection_that_never_opens_is_transient(smtp):
    smtp.fail_with, smtp.fail_on = OSError("unreachable ana@example.com"), "connect"
    with pytest.raises(WorkspaceTransient):
        await spacemail.SpacemailMailer().send_message("x", _mail())


async def test_a_subject_with_a_line_break_is_refused_before_any_connection(smtp):
    bad = OutgoingMail(to=["ana@example.com"], subject="oi\nBcc: x@y.com", body_text="b")
    with pytest.raises(WorkspaceError):
        await spacemail.SpacemailMailer().send_message("x", bad)
    assert smtp.calls == []


async def test_without_credentials_it_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "spacemail_password", "")
    with pytest.raises(WorkspaceNotConfigured):
        await spacemail.SpacemailMailer().send_message("x", _mail())


# ── Who sends ──────────────────────────────────────────────────────────────

def test_spacemail_sends_when_configured(configured, monkeypatch):
    monkeypatch.setattr(settings, "reception_mailbox", "other@example.com")
    assert workspace.mail_sender() is workspace._spacemail
    assert workspace.mail_sender_provider() == "spacemail"
    assert workspace.sender_mailbox() == SENDER


@pytest.mark.parametrize("missing", ["spacemail_username", "spacemail_password"])
def test_the_suite_sends_when_spacemail_is_off(monkeypatch, missing):
    monkeypatch.setattr(settings, "spacemail_username", SENDER)
    monkeypatch.setattr(settings, "spacemail_password", "x")
    monkeypatch.setattr(settings, missing, "")
    monkeypatch.setattr(settings, "reception_mailbox", "reception@example.com")
    sentinel = object()
    monkeypatch.setattr(workspace, "mail", lambda: sentinel)
    assert workspace.mail_sender() is sentinel
    assert workspace.sender_mailbox() == "reception@example.com"
    assert workspace.mail_sender_provider() in ("google", "microsoft")


@pytest.mark.parametrize("address", ["x@y.com\r\nRCPT TO:<evil@z.com>", "sem-arroba",
                                     "a b@y.com", "<a@y.com>", "a@y.com,b@y.com", "ana@@example.com"])
async def test_a_malformed_recipient_is_refused_before_any_connection(smtp, address):
    for field in ("to", "cc", "bcc"):
        mail = _mail().model_copy(update={field: [address]})
        with pytest.raises(WorkspaceError) as caught:
            await spacemail.SpacemailMailer().send_message("x", mail)
        assert "evil" not in str(caught.value) and smtp.calls == []


async def test_a_partial_refusal_still_counts_as_sent_and_is_logged_without_addresses(
        smtp, caplog):
    smtp.refused = {"cc@example.com": (550, b"no such user")}
    with caplog.at_level("WARNING"):
        sent_id = await spacemail.SpacemailMailer().send_message(
            "x", _mail(cc=["cc@example.com"]))
    assert sent_id.startswith("<")
    assert "refused 1 of 2" in caplog.text and "example.com" not in caplog.text
