"""The TESTE mark on what the team reads about a contact in /teste.

Pure on purpose: every place that writes to the team group or names a calendar
event passes its text through here, and none of them should have to know what
a test is beyond the contact in hand."""
from __future__ import annotations

from core import content
from core.models import Contact


def team_text(text: str, contact: Contact | None) -> str:
    """A line for the team, with "🧪 TESTE · " in front when it is about a test."""
    if contact is not None and contact.test_mode:
        # The content loader trims each message, so the space after the mark
        # is put back here.
        return f"{content.text('teste.team_prefix').rstrip()} {text}"
    return text


def quiet(contact: Contact | None) -> bool:
    """A test (/teste) is not announced in the team group: what happens in it
    is read in the Inbox, and the group is for customers."""
    return contact is not None and bool(contact.test_mode)


def event_title(title: str, contact: Contact) -> str:
    """The calendar event's title, starting with "[TESTE] " for a test."""
    return f"[TESTE] {title}" if contact.test_mode else title
