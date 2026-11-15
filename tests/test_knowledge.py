"""The desk answering only from the organization's own FAQ.

The property under test is a refusal, not an answer: when nothing is retrieved,
or when the retrieval itself fails, the desk must say it does not know and hand
the question to a person. A model given an empty context will not decline — it
will invent a price, fluently, in the clinic's own voice.
"""
from pathlib import Path

import pytest

from core import content
from core.config import settings
from core.models import Triage
from core.services import graph, knowledge

FAQ = """# Título

## Qual o preço da consulta dermatológica?

R$ 350,00 com a Dra. Camila Prado, 45 minutos.

## Onde vocês ficam?

Av. Conselheiro Furtado, 1420, sala 502.

## Uma seção sem resposta
"""


# ── Chunking ─────────────────────────────────────────────────────────────────

def test_each_question_becomes_one_chunk():
    chunks = knowledge.split(FAQ)
    assert [chunk["heading"] for chunk in chunks] == [
        "Qual o preço da consulta dermatológica?",
        "Onde vocês ficam?",
    ]


def test_a_heading_with_no_answer_is_not_stored():
    """An empty section retrieves as well as a full one and answers nothing."""
    assert all(chunk["body"] for chunk in knowledge.split(FAQ))


def test_the_price_stays_with_the_service_it_belongs_to():
    """Chunking anywhere but the heading is how R$ 350,00 ends up attached to
    the wrong procedure."""
    chunks = knowledge.split(FAQ)
    price = next(c for c in chunks if "preço" in c["heading"])
    assert "R$ 350,00" in price["body"] and "Camila" in price["body"]


def test_the_real_faq_chunks_cleanly():
    chunks = knowledge.split(
        Path("content/faq.md").read_text(encoding="utf-8")
    )
    assert len(chunks) == 81
    # The Converge CRM product is gone: no plan, no price, no answer that sells it.
    assert not any("R$ 549,90" in c["body"] or "planos e preços" in c["heading"] for c in chunks)
    crm = next(c for c in chunks if c["heading"].startswith("A Converge tem um CRM"))
    assert "não vende um CRM pronto" in crm["body"]
    protection = next(c for c in chunks if c["heading"].startswith("Como a Converge protege os dados"))
    assert "LGPD" in protection["body"] and "AES-256" in protection["body"]
    assert "em repouso" in protection["body"]
    assert "pela própria aplicação" in protection["body"]
    payment = next(c for c in chunks if "formas de pagamento" in c["heading"])
    assert "Pix" in payment["body"]


async def test_a_document_with_no_headings_is_an_error_not_an_empty_base():
    """Silently storing zero chunks leaves a desk that refuses every question
    and no sign of why."""
    with pytest.raises(ValueError, match="headings"):
        await knowledge.ingest(None, "sem cabeçalhos aqui", "x.md")


# ── Retrieval ────────────────────────────────────────────────────────────────

def _base(monkeypatch: pytest.MonkeyPatch, chunks, question_vector):
    async def all_chunks(conn, source=None):
        return chunks

    async def fake_embed(texts):
        return [question_vector] if question_vector else None

    monkeypatch.setattr(knowledge.knowledge_repo, "all_chunks", all_chunks)
    monkeypatch.setattr(knowledge, "embed", fake_embed)


async def test_retrieval_returns_the_closest_chunk_first(monkeypatch: pytest.MonkeyPatch):
    _base(
        monkeypatch,
        [
            {"heading": "preço", "body": "R$ 350", "embedding": [1.0, 0.0]},
            {"heading": "endereço", "body": "Rua A", "embedding": [0.0, 1.0]},
        ],
        [1.0, 0.0],
    )
    found = await knowledge.retrieve(None, "quanto custa?")
    assert found[0]["heading"] == "preço"


async def test_a_chunk_below_the_floor_is_not_retrieved(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "knowledge_min_similarity", 0.9)
    _base(
        monkeypatch,
        [{"heading": "endereço", "body": "Rua A", "embedding": [0.0, 1.0]}],
        [1.0, 0.0],
    )
    assert await knowledge.retrieve(None, "quanto custa?") == []


async def test_an_empty_knowledge_base_retrieves_nothing(
    monkeypatch: pytest.MonkeyPatch
):
    _base(monkeypatch, [], [1.0, 0.0])
    assert await knowledge.retrieve(None, "qualquer coisa") == []


async def test_a_failed_embedding_retrieves_nothing_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch
):
    """Answering from memory is exactly the failure mode this prevents."""
    _base(monkeypatch, [{"heading": "h", "body": "b", "embedding": [1.0]}], None)
    assert await knowledge.retrieve(None, "quanto custa?") == []


# ── The refusal, at the answering node ───────────────────────────────────────

def _never_called(*args, **kwargs):
    raise AssertionError("The model must not be asked without retrieved text.")


async def test_nothing_retrieved_means_the_desk_says_it_does_not_know(
    monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(graph, "complete", _never_called)

    async def retrieve(question):
        return []

    state = graph.DeskState(
        message="qual o preço da rinoplastia?", chat_id="1",
        triage=Triage(intent="faq", confidence=0.9), retrieve=retrieve,
    )
    result = await graph._answer(state)
    reply = result["reply"]
    assert reply.text == content.text("reply.unknown")
    assert reply.needs_human is True


async def test_the_retrieved_text_is_what_the_model_is_given(
    monkeypatch: pytest.MonkeyPatch
):
    seen = {}

    async def fake_complete(system, user, **kwargs):
        seen["system"] = system
        seen["user"] = user
        return "A consulta custa R$ 350,00."

    monkeypatch.setattr(graph, "complete", fake_complete)

    async def retrieve(question):
        return [{"heading": "preço", "body": "R$ 350,00 com a Dra. Camila Prado."}]

    state = graph.DeskState(
        message="quanto custa?", chat_id="1",
        triage=Triage(intent="faq", confidence=0.9), retrieve=retrieve,
    )
    await graph._answer(state)

    assert "R$ 350,00 com a Dra. Camila Prado." in seen["user"]
    # And the prompt carries the organization's prohibitions, not generic ones.
    assert "diagn" in seen["system"].lower()
    assert content.text("reply.unknown") in seen["system"]


async def test_a_model_that_fails_hands_the_question_to_a_person(
    monkeypatch: pytest.MonkeyPatch
):
    async def fails(system, user, **kwargs):
        return None

    monkeypatch.setattr(graph, "complete", fails)

    async def retrieve(question):
        return [{"heading": "h", "body": "b"}]

    state = graph.DeskState(
        message="quanto custa?", chat_id="1",
        triage=Triage(intent="faq", confidence=0.9), retrieve=retrieve,
    )
    reply = (await graph._answer(state))["reply"]
    assert reply.text == content.text("reply.unknown")
    assert reply.needs_human is True


async def test_escalation_uses_the_organizations_own_words():
    state = graph.DeskState(
        message="quero falar com alguém", chat_id="1",
        triage=Triage(intent="escalation", confidence=0.9),
    )
    reply = (await graph._answer(state))["reply"]
    assert reply.text == content.text("reply.escalation")
    assert reply.needs_human is True
