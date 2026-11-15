"""The preliminary quote: the questionnaire's button answers read back into
the table's inputs, and the table's quote. The model never prices anything."""
from pathlib import Path

import pytest

from core import content
from core.config import settings
from core.services import pricing, proposal_quote
from core.services.pricing import System

SHIPPED = Path(__file__).resolve().parent.parent / "content"


@pytest.fixture(autouse=True)
def table(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "content_dir", str(SHIPPED))
    pricing.reset()
    content.reset()
    loaded = pricing.load_table()
    assert loaded is not None
    yield loaded
    pricing.reset()
    content.reset()


def t(key: str) -> str:
    return content.text(f"proposal.{key}")


def _biz(**extra: str) -> dict[str, str]:
    answers = {
        "canais_lista": "WhatsApp, Instagram",
        "volume": t("volume_ate_2000"),
        "areas": f"{t('biz_area_vendas')}, {t('biz_area_atendimento')}",
        "sistemas_lista": f"{t('sistema_google_agenda')}, {t('sistema_planilha')}",
        "processo": t("processo_sim"),
        "usuarios_faixa": t("usuarios_3_5"),
    }
    answers.update(extra)
    return answers


def test_empresa_completa_vira_answers():
    answers, typed = proposal_quote.answers_for(_biz(), clinic=False)
    assert answers == pricing.Answers(
        canais=("WhatsApp", "Instagram"), volume_faixa="ate_2000",
        areas=("vendas", "atendimento"),
        sistemas=(System("google agenda"), System("planilha google")),
        roteiro_definido=True, usuarios=5)
    assert typed == ()


def test_clinica_mapeia_areas_e_dado_sensivel():
    areas = ", ".join(t(k) for k in ("area_agenda", "area_triagem", "area_prontuario"))
    answers, _ = proposal_quote.answers_for(_biz(areas=areas), clinic=True)
    assert answers.areas == ("atendimento", "documentos_juridico")
    assert answers.dado_sensivel is True
    assert answers.documentos == "pequeno"


def test_clinica_sem_documentos_usa_padrao():
    answers, _ = proposal_quote.answers_for(_biz(areas=t("area_agenda")), clinic=True)
    assert answers.documentos is None
    assert answers.dado_sensivel is True


def test_area_digitada_vira_premissa():
    areas = f"{t('biz_area_vendas')}, Controle de estoque"
    answers, typed = proposal_quote.answers_for(_biz(areas=areas), clinic=False)
    assert answers.areas == ("vendas",)
    assert typed == ("Controle de estoque",)
    computed = proposal_quote.compute(_biz(areas=areas), clinic=False)
    assert isinstance(computed, proposal_quote.Computed)
    assert "área não calculada: Controle de estoque" in computed.premissas


def test_nenhum_sistema_e_lista_vazia():
    answers, _ = proposal_quote.answers_for(_biz(sistemas_lista=t("sistema_nenhum")),
                                            clinic=False)
    assert answers.sistemas == ()


def test_sistema_digitado_entra_pelo_nome():
    answers, _ = proposal_quote.answers_for(_biz(sistemas_lista="HubSpot"), clinic=False)
    assert answers.sistemas == (System("HubSpot"),)


def test_crm_sem_nome_e_complexa():
    computed = proposal_quote.compute(_biz(sistemas_lista=t("sistema_crm")), clinic=False)
    assert isinstance(computed, proposal_quote.Computed)
    assert computed.scope.complexas == 1
    assert computed.scope.simples == 0


def test_processo_mais_ou_menos_nao_e_mapeado():
    computed = proposal_quote.compute(_biz(processo=t("processo_parcial")), clinic=False)
    assert isinstance(computed, proposal_quote.Computed)
    assert computed.scope.processo == "nao_mapeado"


def test_pulado_vira_faltam():
    answers = _biz(volume="")
    del answers["processo"]
    assert proposal_quote.compute(answers, clinic=False) == proposal_quote.Missing(
        faltam=("volume", "roteiro"))


def test_volume_digitado_vira_faltam():
    assert proposal_quote.compute(_biz(volume="umas mil"), clinic=False) == \
        proposal_quote.Missing(faltam=("volume",))


def test_sem_tabela_nao_calcula(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    answers = _biz()  # the labels, read while the shipped content is loaded
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    pricing.reset()
    assert proposal_quote.compute(answers, clinic=False) is None


def test_usuarios_pulado_usa_padrao():
    answers = _biz()
    del answers["usuarios_faixa"]
    computed = proposal_quote.compute(answers, clinic=False)
    assert isinstance(computed, proposal_quote.Computed)
    assert "usuarios" in computed.premissas


def test_orcamento_e_o_da_tabela(table):
    computed = proposal_quote.compute(_biz(), clinic=False)
    assert isinstance(computed, proposal_quote.Computed)
    expected = pricing.quote(pricing.derive(computed.answers, table), table)
    assert computed.quote == expected
