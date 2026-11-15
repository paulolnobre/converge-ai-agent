"""The quote: every coefficient from the table, cents throughout, half-up to
the multiple the table names — the two examples of the spec, to the real."""
import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest

from core.config import settings
from core.services import pricing
from core.services.pricing import Scope

SHIPPED = Path(__file__).resolve().parent.parent / "content"


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "content_dir", str(SHIPPED))
    pricing.reset()
    loaded = pricing.load_table()
    assert loaded is not None
    yield loaded
    pricing.reset()


BASIC = Scope(complexidade="basico", canais=1, simples=0, complexas=0, documentos="nenhum",
              infra="compartilhada", processo="mapeado", conversas=1000, usuarios=2, sla=False,
              premissas=())


def test_basic_example(table):
    quote = pricing.quote(BASIC, table)
    assert (quote.setup_cents, quote.setup_low_cents, quote.setup_high_cents, quote.monthly_cents) == (
        250000, 240000, 280000, 102000)
    assert quote.table_digest == table.digest


def test_rag_example(table):
    scope = dataclasses.replace(BASIC, complexidade="intermediario_rag", canais=2, simples=1,
                                documentos="pequeno", conversas=2000, usuarios=3)
    quote = pricing.quote(scope, table)
    assert (quote.setup_cents, quote.setup_low_cents, quote.setup_high_cents, quote.monthly_cents) == (
        560000, 530000, 620000, 174000)


def test_small_volume_pays_the_traffic_minimum(table):
    quote = pricing.quote(dataclasses.replace(BASIC, conversas=250), table)
    assert quote.detail["trafego"] == 20000
    assert quote.monthly_cents == 40000 + 20000 + 2 * 6000


def test_factors_multiply(table):
    scope = dataclasses.replace(BASIC, complexidade="multi_agente", canais=3, complexas=2,
                                documentos="grande", infra="vpc_cliente", processo="nao_mapeado")
    assert pricing.quote(scope, table).setup_cents == 2340000


def test_sla_and_users_add_to_monthly(table):
    scope = dataclasses.replace(BASIC, sla=True, usuarios=4)
    assert pricing.quote(scope, table).monthly_cents == 102000 + 70000 + 2 * 6000


def test_half_rounds_up():
    assert pricing.round_to(Decimal(535000), 10000) == 540000
    assert pricing.round_to(Decimal(534999), 10000) == 530000


def test_monthly_uses_the_original_setup(table):
    big = dataclasses.replace(BASIC, complexidade="multi_agente")
    quote = pricing.quote(big, table)
    assert quote.detail["sustentacao"] == max(40000, int(quote.setup_cents * Decimal("0.10")))


def test_nothing_priced_is_hardcoded(table):
    doubled = dataclasses.replace(table, por_canal=table.por_canal * 2)
    assert pricing.quote(BASIC, doubled).setup_cents == 250000 + 50000


def test_the_detail_stays_internal(table):
    quote = pricing.quote(BASIC, table)
    assert set(quote.detail) == {"base", "canais_e_integracoes", "documentos", "fator",
                                 "sustentacao", "trafego", "usuarios", "sla"}
    assert quote.detail["fator"] == "1.00"
