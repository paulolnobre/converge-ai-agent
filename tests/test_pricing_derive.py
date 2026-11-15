"""From what the customer said to the scope the table prices: fixed rules,
never a guess — an unknown system counts as hard, a known one counts once."""
from pathlib import Path

import pytest

from core.config import settings
from core.services import pricing
from core.services.pricing import Answers, System

SHIPPED = Path(__file__).resolve().parent.parent / "content"


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "content_dir", str(SHIPPED))
    pricing.reset()
    loaded = pricing.load_table()
    assert loaded is not None
    yield loaded
    pricing.reset()


def _answers(**overrides) -> Answers:
    values: dict = dict(canais=("WhatsApp",), sistemas=(), areas=("atendimento",),
                        volume=1000, roteiro_definido=True)
    values.update(overrides)
    return Answers(**values)


def test_missing_in_order():
    assert pricing.missing(Answers()) == ["canais", "sistemas", "volume", "areas", "roteiro"]
    assert "volume" not in pricing.missing(Answers(volume_faixa="ate_500"))
    assert pricing.missing(_answers()) == []


def test_known_systems_are_simple_and_counted_once(table):
    scope = pricing.derive(_answers(sistemas=(System("Google Agenda"), System("google calendar"),
                                              System("RD Station "))), table)
    assert (scope.simples, scope.complexas) == (2, 0)


@pytest.mark.parametrize("system", [System("SistemaX"), System("Outlook antigo, sem API"),
                                    System("Outlook", sem_api=True), System("nosso sistema próprio")])
def test_unknown_or_without_api_is_complex(table, system):
    scope = pricing.derive(_answers(sistemas=(system,)), table)
    assert (scope.simples, scope.complexas) == (0, 1)


def test_the_same_unknown_system_counts_once(table):
    scope = pricing.derive(_answers(sistemas=(System("SistemaX"), System(" sistemax"))), table)
    assert scope.complexas == 1


def test_no_system_at_all(table):
    assert pricing.missing(_answers(sistemas=())) == []
    scope = pricing.derive(_answers(sistemas=()), table)
    assert (scope.simples, scope.complexas) == (0, 0)


def test_complexity_rules(table):
    two_areas = pricing.derive(_answers(areas=("atendimento", "financeiro"), documentos="grande"), table)
    with_docs = pricing.derive(_answers(documentos="pequeno"), table)
    plain = pricing.derive(_answers(), table)
    assert (two_areas.complexidade, with_docs.complexidade, plain.complexidade) == (
        "multi_agente", "intermediario_rag", "basico")


def test_the_same_area_twice_is_one_area(table):
    assert pricing.derive(_answers(areas=("atendimento", "atendimento")), table).complexidade == "basico"


def test_an_unknown_area_is_a_clear_error(table):
    with pytest.raises(ValueError, match="área"):
        pricing.derive(_answers(areas=("marketing",)), table)


def test_infra_precedence(table):
    both = pricing.derive(_answers(nuvem_propria=True, dado_sensivel=True), table)
    sensitive = pricing.derive(_answers(dado_sensivel=True), table)
    plain = pricing.derive(_answers(), table)
    assert (both.infra, sensitive.infra, plain.infra) == ("vpc_cliente", "dedicada_lgpd", "compartilhada")


def test_process(table):
    assert pricing.derive(_answers(roteiro_definido=False), table).processo == "nao_mapeado"
    assert pricing.derive(_answers(), table).processo == "mapeado"


def test_volume_from_band(table):
    assert pricing.derive(_answers(volume=None, volume_faixa="mais_5000"), table).conversas == 7000
    assert pricing.derive(_answers(volume=1800), table).conversas == 1800


def test_unknown_band_is_a_clear_error(table):
    with pytest.raises(ValueError, match="volume"):
        pricing.derive(_answers(volume=None, volume_faixa="muitas"), table)


def test_missing_answers_are_refused(table):
    with pytest.raises(ValueError, match="canais"):
        pricing.derive(Answers(), table)


def test_defaults_are_listed_as_assumptions(table):
    scope = pricing.derive(_answers(), table)
    assert (scope.usuarios, scope.documentos, scope.sla) == (2, "nenhum", False)
    assert scope.premissas == ("usuarios", "documentos", "sla")
    assert "usuarios" not in pricing.derive(_answers(usuarios=5), table).premissas


def test_channels_counted_once(table):
    assert pricing.derive(_answers(canais=("WhatsApp", "whatsapp ", "Instagram")), table).canais == 2


@pytest.mark.parametrize("systems", [
    (System("Outlook"), System("Outlook", sem_api=True)),
    (System("Outlook"), System("Outlook antigo, sem API")),
    (System("Gmail", sem_api=True), System("gmail", sem_api=True)),
])
def test_the_same_system_counts_once_and_the_hard_kind_wins(table, systems):
    scope = pricing.derive(_answers(sistemas=systems), table)
    assert (scope.simples, scope.complexas) == (0, 1)
