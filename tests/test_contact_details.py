"""E-mail and phone read by pattern: every real format, and nothing that only
looks like a number — a date, a time, a CPF or a price must never become a phone."""
import pytest

from core.services.contact_details import extract, missing


@pytest.mark.parametrize("text, email, phone", [
    ("paulo@x.com, 91 90000-0001", "paulo@x.com", "+55 91 90000-0001"),
    ("meu e-mail é Paulo.Nobre@Converge.com.br e o telefone (91) 90000-0001",
     "paulo.nobre@converge.com.br", "+55 91 90000-0001"),
    ("+55 91 90000-0001", None, "+55 91 90000-0001"),
    ("91900000001", None, "+55 91 90000-0001"),
    ("fixo: (91) 3223-4567", None, "+55 91 3223-4567"),
])
def test_real_formats_are_read_and_normalised(text, email, phone):
    found = extract(text)
    assert (found.email, found.phone) == (email, phone)
    assert found.only_details


@pytest.mark.parametrize("text", [
    "quero marcar dia 24/09 às 09:30",
    "meu cpf 123.456.789-00",
    "quanto custa? R$ 749,90",
    "2026-09-24",
    "91 12345-6789",   # nine digits not starting with 9: not a mobile
    "05 90000-0001",   # no DDD has a zero
])
def test_things_that_only_look_like_numbers_are_left_alone(text):
    assert not extract(text).any


def test_details_with_a_question_are_not_only_details():
    """Saved, but the question still deserves its answer."""
    found = extract("paulo@x.com e quanto custa o plano pro?")
    assert found.email == "paulo@x.com" and not found.only_details


def test_missing_names_what_is_still_needed():
    assert missing(None, None) == ("email", "phone")
    assert missing("a@b.co", None) == ("phone",)
    assert missing("a@b.co", "+55 91 90000-0001") == ()
