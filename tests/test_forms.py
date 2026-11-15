"""The WhatsApp Flows kept in content/forms: read by their payload keys and
option ids (the contract with the code), and checked at deploy. Titles, order
and screens are the owner's to change."""
import json
import shutil
from pathlib import Path

import pytest

from core.config import settings
from core.services import forms

SHIPPED = Path(__file__).resolve().parent.parent / "content"


@pytest.fixture(autouse=True)
def _fresh():
    forms.reset()
    yield
    forms.reset()


@pytest.fixture
def shipped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "content_dir", str(SHIPPED))
    forms.reset()


@pytest.fixture
def mine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A copy of the shipped forms, to break one thing at a time."""
    shutil.copytree(SHIPPED / "forms", tmp_path / "forms")
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    forms.reset()
    return tmp_path / "forms"


def _edit(path: Path, change) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _children(data: dict) -> list[dict]:
    """Every component of the Flow, screen by screen; the last is the final button."""
    return [c for screen in data["screens"] for c in screen["layout"]["children"]]


def test_the_shipped_forms_match_the_local_contract(shipped):
    assert forms.problems() == []


def test_a_form_without_a_flow_id_is_not_sent(mine: Path):
    (mine / "flows.toml").write_text('[proposta_atendimento]\nflow_id = ""\n', encoding="utf-8")
    form = forms.load("proposta_atendimento")
    assert form is not None and form.flow_id is None


def test_a_flow_id_is_read(mine: Path):
    (mine / "flows.toml").write_text('[proposta_atendimento]\nflow_id = "123456"\n',
                                     encoding="utf-8")
    form = forms.load("proposta_atendimento")
    assert form is not None and form.flow_id == "123456"
    clinic = forms.load("proposta_clinica")
    assert clinic is not None and clinic.flow_id is None


def test_the_same_flow_id_twice_refuses_the_deployment(mine: Path):
    (mine / "flows.toml").write_text(
        '[proposta_atendimento]\nflow_id = "111"\n[proposta_clinica]\nflow_id = " 111 "\n',
        encoding="utf-8")
    assert any("111" in p and "proposta_clinica" in p for p in forms.problems())


def test_fields_and_prefill_are_read(shipped):
    form = forms.load("proposta_atendimento")
    assert form is not None and form.screen == "SOBRE"
    assert form.prefill == ("nome", "empresa", "ramo", "cargo", "email")
    assert form.fields["areas"].kind == "varias"
    assert form.fields["volume"].kind == "uma"
    assert form.fields["nome"].kind == "texto"
    assert form.fields["volume"].options == ("ate_500", "ate_2000", "ate_5000", "mais_5000")


def test_the_scheduling_flow_takes_its_times_from_the_message(shipped):
    form = forms.load("agendamento")
    assert form is not None and form.screen == "AGENDAR"
    assert form.prefill == ("nome", "email", "horarios")
    assert form.fields["horario"].kind == "uma" and form.fields["horario"].options is None


def test_a_missing_payload_key_refuses_the_deployment(mine: Path):
    def drop(data):
        footer = _children(data)[-1]
        del footer["on-click-action"]["payload"]["email"]
    _edit(mine / "proposta_atendimento.json", drop)
    problems = forms.problems()
    assert any("proposta_atendimento" in p and "email" in p for p in problems)


def test_an_unknown_option_id_refuses_the_deployment(mine: Path):
    def rename(data):
        for child in _children(data):
            if child.get("name") == "volume":
                child["data-source"][0]["id"] = "500"
    _edit(mine / "proposta_atendimento.json", rename)
    problems = forms.problems()
    assert any("volume" in p and "500" in p for p in problems)


def test_a_changed_title_is_not_a_problem(mine: Path):
    def retitle(data):
        for child in _children(data):
            if child.get("name") == "areas":
                child["label"] = "Outro texto"
                child["data-source"][0]["title"] = "Atender e agendar"
    _edit(mine / "proposta_atendimento.json", retitle)
    assert forms.problems() == []


def test_a_payload_key_may_come_from_a_differently_named_component(mine: Path):
    def rename(data):
        children = _children(data)
        for child in children:
            if child.get("name") == "prazo":
                child["name"] = "quando"
        children[-1]["on-click-action"]["payload"]["prazo"] = "${screen.FECHAMENTO.form.quando}"
    _edit(mine / "proposta_atendimento.json", rename)
    assert forms.problems() == []
    form = forms.load("proposta_atendimento")
    assert form is not None and form.fields["prazo"].kind == "texto"


def test_a_broken_json_refuses_the_deployment(mine: Path):
    (mine / "proposta_clinica.json").write_text("{ nope", encoding="utf-8")
    assert any("proposta_clinica" in p for p in forms.problems())


def test_a_flow_without_a_complete_action_refuses_the_deployment(mine: Path):
    def nothing(data):
        _children(data)[-1]["on-click-action"]["name"] = "navigate"
    _edit(mine / "agendamento.json", nothing)
    assert any("agendamento" in p for p in forms.problems())


def test_a_broken_flows_file_refuses_the_deployment(mine: Path):
    (mine / "flows.toml").write_text("isto não é toml [", encoding="utf-8")
    assert any("flows.toml" in p for p in forms.problems())


def test_no_forms_folder_is_not_a_problem(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    forms.reset()
    assert forms.problems() == [] and forms.load("proposta_atendimento") is None


# ── What the contract refuses ───────────────────────────────────────────────

def _field(data: dict, name: str) -> dict:
    return next(c for c in _children(data) if c.get("name") == name)


def test_a_literal_in_the_payload_refuses_the_deployment(mine: Path):
    def literal(data):
        _children(data)[-1]["on-click-action"]["payload"]["email"] = "literal"
    _edit(mine / "proposta_atendimento.json", literal)
    assert any("email" in p and "componente" in p for p in forms.problems())


def test_an_unsupported_reference_refuses_the_deployment(mine: Path):
    def data_ref(data):
        _children(data)[-1]["on-click-action"]["payload"]["prazo"] = "${data.prazo}"
    _edit(mine / "proposta_atendimento.json", data_ref)
    assert any("prazo" in p for p in forms.problems())


@pytest.mark.parametrize("field, new_type", [("areas", "RadioButtonsGroup"),
                                              ("volume", "CheckboxGroup"),
                                              ("nome", "RadioButtonsGroup")])
def test_a_field_that_changes_how_it_is_answered_refuses_the_deployment(
        mine: Path, field: str, new_type: str):
    def retype(data):
        component = _field(data, field)
        component["type"] = new_type
        component.setdefault("data-source", [{"id": "x", "title": "X"}])
    _edit(mine / "proposta_atendimento.json", retype)
    assert any(field in p for p in forms.problems())


def test_a_list_that_should_be_fixed_cannot_come_from_the_message(mine: Path):
    def dynamic(data):
        data["screens"][1]["data"] = {"lista": {
            "type": "array", "items": {"type": "object"}, "__example__": []}}
        _field(data, "volume")["data-source"] = "${data.lista}"
    _edit(mine / "proposta_atendimento.json", dynamic)
    assert any("volume" in p and "opções fixas" in p for p in forms.problems())


def test_a_list_that_should_come_from_the_message_cannot_be_fixed(mine: Path):
    def fixed(data):
        _field(data, "horario")["data-source"] = [{"id": "0", "title": "Seg 09:00"}]
    _edit(mine / "agendamento.json", fixed)
    assert any("horario" in p and "da mensagem" in p for p in forms.problems())


def test_a_dynamic_source_nobody_declares_refuses_the_deployment(mine: Path):
    def undeclared(data):
        _field(data, "horario")["data-source"] = "${data.nao_existe}"
    _edit(mine / "agendamento.json", undeclared)
    assert any("agendamento" in p and "nao_existe" in p for p in forms.problems())


def test_a_repeated_option_id_refuses_the_deployment(mine: Path):
    def repeat(data):
        source = _field(data, "pagamento")["data-source"]
        source[1]["id"] = source[0]["id"]
    _edit(mine / "proposta_atendimento.json", repeat)
    assert any("pagamento" in p and "repet" in p for p in forms.problems())


@pytest.mark.parametrize("change", [
    lambda d: d.update(screens=[None]),
    lambda d: d.update(screens="nada"),
    lambda d: d["screens"][0].pop("layout"),
    lambda d: d["screens"][0]["layout"].update(children={"não": "é lista"}),
    lambda d: _field(d, "areas")["data-source"].append(None),
    lambda d: _children(d)[-1]["on-click-action"].update(payload=["a"]),
    lambda d: d["screens"][0].update(data=[1]),
])
def test_a_malformed_flow_is_reported_not_raised(mine: Path, change):
    _edit(mine / "proposta_atendimento.json", change)
    problems = forms.problems()
    assert problems and all(isinstance(p, str) for p in problems)
    assert any("proposta_atendimento" in p for p in problems)


def test_a_flow_split_in_screens_keeps_the_contract(shipped):
    form = forms.load("proposta_atendimento")
    assert form is not None and form.screen == "SOBRE"
    assert form.prefill == ("nome", "empresa", "ramo", "cargo", "email")
    assert form.fields["areas"].kind == "varias" and form.fields["processo"].kind == "uma"
    clinic = forms.load("proposta_clinica")
    assert clinic is not None and clinic.prefill == ("nome", "empresa", "cargo", "email")
