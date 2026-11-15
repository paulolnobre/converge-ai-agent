"""The lead form's message is read by code and answered from what it says."""
import pytest

from core import content
from core.services import form_lead

# The shape Meta sends, answers in either order.
FORM = """Olá! Preenchi seu formulário e gostaria de saber mais sobre sua empresa.

Qual é a sua função na clínica?: Proprietário ou sócio
Full name: izabel Ataide
Phone number: +5591999990000
Qual é o nome da clínica?: Clínica Bem Saúde
Aproximadamente quantas mensagens a clínica recebe por dia?: Até 20
Email: izabel@example.com
Qual é hoje o principal desafio no atendimento?: {desafio}"""


@pytest.mark.parametrize("desafio, key", [
    ("Perda de agendamentos", "form.challenge_bookings"),
    ("Falta de organização dos contatos", "form.challenge_organization"),
    ("Demora para responder", "form.challenge_slow"),
    ("Outro", "form.challenge_other"),
])
def test_the_challenge_picks_the_bridge(desafio, key):
    lead = form_lead.read(FORM.format(desafio=desafio))

    assert lead == form_lead.FormLead(name="izabel Ataide", challenge=key,
                                      clinic="Clínica Bem Saúde", role="Proprietário ou sócio",
                                      volume="Até 20")
    assert lead is not None
    text = form_lead.opening(lead, "Bel 🌸")
    assert text.startswith(content.render("form.hello", nome="Izabel"))
    assert content.text(key) in text and text.endswith(content.text("form.next"))


def test_without_a_name_the_profile_name_then_none():
    body = FORM.format(desafio="Outro").replace("Full name: izabel Ataide\n", "")
    lead = form_lead.read(body)
    assert lead is not None
    assert form_lead.opening(lead, "Bel").startswith(content.render("form.hello", nome="Bel"))
    assert form_lead.opening(lead, None).startswith(content.text("form.hello_anon"))


@pytest.mark.parametrize("body", [
    "Olá, quero saber mais sobre a empresa",
    "Preenchi seu formulário",  # the opening alone, no answers
    "Nome: Ana\nDesafio: demora",  # answers without the form's opening
])
def test_anything_else_is_not_the_form(body):
    assert form_lead.read(body) is None


def test_the_opening_never_offers_other_products():
    for key in ("form.challenge_bookings", "form.challenge_organization",
                "form.challenge_slow", "form.challenge_other", "form.how_it_works"):
        text = content.text(key)
        assert "Jurídico" not in text and "Construction" not in text


LAWYER_FORM = """Olá! Preenchi seu formulário e gostaria de saber mais sobre sua empresa.

Full name: Carlos Mendes
Phone number: +5591977776666
Qual é o nome do escritório?: Mendes Advocacia
Email: carlos@example.com
Qual é hoje o principal desafio do escritório?: Demora para responder"""


def test_the_lawyer_form_is_read_but_not_as_a_clinic():
    lead = form_lead.read(LAWYER_FORM)
    assert lead is not None and not lead.for_clinic
    clinic = form_lead.read(FORM.format(desafio="Outro"))
    assert clinic is not None and clinic.for_clinic


_PLAIN = """Olá! Preenchi seu formulário e gostaria de saber mais sobre sua empresa.

Full name: Rita Souza
Email: rita@example.com
Qual é a sua área de atuação?: {area}"""


@pytest.mark.parametrize("area, product", [
    ("Sou dentista, tenho um consultório", "clinic"),
    ("Sou advogado", "legal"),
    ("Escritório de advocacia", "legal"),
    ("Escritório com 5 sócios", "legal"),  # "escritório" alone, nothing else
    ("Construtora de obras residenciais", "construction"),
    ("Engenharia civil", "construction"),
    ("Varejo de roupas", "general"),
    ("Advocacia e engenharia", "general"),  # two products: unclear
    ("Escritório de engenharia", "construction"),
])
def test_the_product_is_read_from_what_they_wrote(area, product):
    lead = form_lead.read(_PLAIN.format(area=area))
    assert lead is not None and lead.product == product


def test_each_product_gets_its_own_opening_never_the_clinic():
    for area, key in (("Construtora", "form.construction_pitch"),
                      ("Varejo", "form.general"),
                      ("Advocacia", "form.legal_pitch")):
        lead = form_lead.read(_PLAIN.format(area=area))
        assert lead is not None
        text = form_lead.opening(lead, None)
        assert content.text(key) in text and "clínica" not in text.lower()
