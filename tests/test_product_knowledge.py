"""The shipped FAQ, read as the desk reads it, for each product Converge sells.

The desk states nothing that is not in `content/faq.md`, so the depth of what it
can say about a product is the depth of that file. These tests hold the shape
that depth depends on — enough sections per product, each named after it so
retrieval does not mix two products — and the two promises the text must keep:
no client is ever named, and a product still in development is never described
as one customers already use.
"""
import re

from core import content
from core.config import settings
from core.services import knowledge

# Minimum number of `## <Produto> — …` sections per product.
PRODUCTS = {
    "Atendimento com IA": 7,
    "Assistente Jurídico": 8,
    "Construction Manager": 5,
}

MAX_BODY = 1500
CLIENT_TERMS = re.compile(r"\b(Acme|PJe)\b")
CONSTRUCTION_FORBIDDEN = ["já está em produção", "já em produção", "em uso por"]


def _document() -> str:
    return (content._root() / "faq.md").read_text(encoding="utf-8")


def _chunks() -> list[dict]:
    return knowledge.split(_document())


def _product_chunks(name: str) -> list[dict]:
    return [c for c in _chunks() if c["heading"].startswith(f"{name} — ")]


def test_each_product_has_its_minimum_sections():
    counts = {name: len(_product_chunks(name)) for name in PRODUCTS}
    assert all(counts[name] >= minimum for name, minimum in PRODUCTS.items()), counts


def test_headings_are_unique():
    headings = [c["heading"] for c in _chunks()]
    duplicates = {h for h in headings if headings.count(h) > 1}
    assert not duplicates


def test_no_section_body_exceeds_1500_characters():
    long = {c["heading"]: len(c["body"]) for c in _chunks() if len(c["body"]) > MAX_BODY}
    assert not long


def test_the_faq_never_names_the_reference_client():
    assert CLIENT_TERMS.findall(_document()) == []


def test_construction_manager_is_described_as_in_development():
    # Line wrapping in the markdown is not meaning: compare on single spaces.
    text = " ".join(
        " ".join(f"{c['heading']} {c['body']}".split())
        for c in _product_chunks("Construction Manager")
    )
    assert "em desenvolvimento" in text
    assert "nenhum cliente" in text.lower()
    assert "nunca compra" in text
    assert [p for p in CONSTRUCTION_FORBIDDEN if p in text] == []


def test_top_k_is_wide_enough_for_broad_questions():
    assert settings.knowledge_top_k == 6


# ── Coherence between sections the same question can retrieve together ─────

def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(prefix: str) -> str:
    chunk = next(c for c in _chunks() if c["heading"].startswith(prefix))
    return _flat(chunk["body"])


def test_office_hours_do_not_contradict_the_always_on_desk():
    hours = _section("Qual o horário de atendimento da Converge?")
    assert "a qualquer hora" in hours
    assert "respondida assim que o próximo expediente" not in hours


def test_automations_are_stated_as_telegram_only():
    channels = _section("Atendimento com IA — quais canais atende")
    assert "automações" in channels and "Telegram" in channels.split("automações", 1)[1]


def test_the_agenda_promise_names_google_and_flags_microsoft():
    audience = _section("Atendimento com IA — para quem serve")
    assert "Google Agenda" in audience and "Microsoft 365" in audience


def test_the_product_list_does_not_describe_construction_as_built():
    products = _section("Quais aplicações de agentes de IA")
    construction = products.split("Construction Manager", 1)[1]
    assert " une " not in construction and "vai unir" in construction


def test_no_wording_that_overstates_or_misreads():
    document = _flat(_document())
    assert "lidos por padrão" not in document
    assert "página pública do processo" not in document
