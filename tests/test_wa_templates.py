"""The WhatsApp templates the agent knows: which are approved on this server,
and which the team may send by hand past the 24h window."""
import pytest

from core.config import settings
from core.services import wa_templates


@pytest.fixture
def wa(monkeypatch: pytest.MonkeyPatch):
    for name, value in {"whatsapp_app_secret": "s", "whatsapp_phone_number_id": "PN1",
                        "whatsapp_access_token": "t", "whatsapp_webhook_verify_token": "v"}.items():
        monkeypatch.setattr(settings, name, value)
    for t in wa_templates.CATALOG:
        monkeypatch.setattr(settings, t.setting, None)
    return monkeypatch


def test_every_template_names_a_real_setting():
    keys = [t.key for t in wa_templates.CATALOG]
    assert len(keys) == len(set(keys))
    for t in wa_templates.CATALOG:
        assert hasattr(settings, t.setting), t.setting


def test_only_configured_templates_are_offered(wa):
    assert wa_templates.manual_ready() == []
    wa.setattr(settings, "whatsapp_lead_general_template", "contato_interesse")
    wa.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")

    assert [t.key for t in wa_templates.manual_ready()] == ["team", "lead_general"]


def test_without_whatsapp_nothing_is_ready(wa):
    wa.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")
    wa.setattr(settings, "whatsapp_access_token", None)

    assert wa_templates.manual_ready() == []


def test_flow_templates_are_never_sent_by_hand(wa):
    """A charge or a proposal needs its own data: only its own button sends it."""
    wa.setattr(settings, "whatsapp_charge_template", "cobranca_enviada")
    wa.setattr(settings, "whatsapp_proposal_template", "proposta_converge")

    assert wa_templates.manual_ready() == []
    assert wa_templates.manual("charge") is None and wa_templates.manual("nope") is None


def test_the_resume_template_keeps_its_language(wa):
    wa.setattr(settings, "whatsapp_resume_template", "retomada_de_conversa")
    wa.setattr(settings, "whatsapp_resume_language", "pt_PT")
    wa.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")

    resume = wa_templates.manual("resume")
    assert resume is not None and wa_templates.language(resume) == "pt_PT"
    team = wa_templates.manual("team")
    assert team is not None and wa_templates.language(team) == "pt_BR"


def test_team_and_paid_templates_take_their_own_language(wa):
    """Meta refuses a template sent in a language it was not approved in."""
    wa.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")
    wa.setattr(settings, "whatsapp_team_language", "en")
    wa.setattr(settings, "whatsapp_paid_language", "en")

    team = wa_templates.manual("team")
    assert team is not None and wa_templates.language(team) == "en"
    paid = next(t for t in wa_templates.CATALOG if t.key == "paid")
    assert wa_templates.language(paid) == "en"
