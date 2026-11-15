"""The WhatsApp Flows kept in content/forms.

The owner builds each Flow in Meta's Flow Builder and keeps a copy of its JSON
here (`<id>.json`); `flows.toml` carries the id the Flow got when published.
What binds a Flow to the code is a short contract: the keys of the payload the
Flow sends when it completes, how each is answered (text, one choice, several)
and the ids of its options. Titles, order and the number of screens are free:
a payload value is read as `${form.x}` (a component of the screen that
completes) or `${screen.S.form.x}` (a component of an earlier screen S).
Answers are stored by those keys and ids, never by the text the person saw.

The first screen is where the Flow opens: the keys of its `data` are what a
message can fill in (the person's name and e-mail, the times to choose from).

Optional like the price table: a Flow with no JSON here, or no id in
flows.toml, is simply not sent, and its questions are asked one by one. A
broken file stops the deployment (content.validate), every problem named.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from core.config import settings

FLOWS_FILE = "flows.toml"
Kind = Literal["texto", "uma", "varias"]
_KINDS: dict[str, Kind] = {
    "TextInput": "texto", "TextArea": "texto",
    "RadioButtonsGroup": "uma", "Dropdown": "uma",
    "CheckboxGroup": "varias", "ChipsSelector": "varias",
}
_SAME_SCREEN = re.compile(r"\$\{form\.([A-Za-z0-9_]+)\}")
_OTHER_SCREEN = re.compile(r"\$\{screen\.([A-Za-z0-9_]+)\.form\.([A-Za-z0-9_]+)\}")
_DATA = re.compile(r"\$\{data\.([A-Za-z0-9_]+)\}")


@dataclass(frozen=True)
class Field:
    kind: Kind
    # The ids written in the Flow; None when the list comes with the message.
    options: tuple[str, ...] | None


@dataclass(frozen=True)
class Form:
    id: str
    flow_id: str | None
    screen: str
    # By payload key: what the Flow sends back, when it comes from a component.
    fields: dict[str, Field]
    # Payload keys whose value is not a component (a literal, an expression).
    unresolved: dict[str, str]
    # What the message can fill in where the Flow opens (the first screen's data).
    prefill: tuple[str, ...]


@dataclass(frozen=True)
class Need:
    """What the code needs of one payload key."""

    kind: Kind
    ids: tuple[str, ...] = ()        # the option ids it knows (static lists)
    from_message: bool = False       # the list comes with the message instead


def required() -> dict[str, dict[str, Need]]:
    """Per Flow, the payload keys the code reads. Here, not at the top: these
    modules read the forms too."""
    from core.services import pricing, proposal_briefing, proposal_quote

    asked = {
        "canais": Need("varias", proposal_quote.CANAIS),
        "volume": Need("uma", proposal_quote.VOLUMES),
        "processo": Need("uma", proposal_quote.PROCESSOS),
        "usuarios": Need("uma", tuple(proposal_quote.USUARIOS)),
        "pagamento": Need("uma", proposal_briefing.PAYMENT_METHODS),
    }
    text = Need("texto")
    who = {"nome": text, "empresa": text, "cargo": text}
    company = {**asked, **who, "ramo": text, "sistemas": text,
               "areas": Need("varias", pricing.AREAS), "custo_hoje": text, "prazo": text,
               "email": text, "gargalos": text, "pedidos": text}
    clinic = {**asked, **who, "sistemas": text, "areas": Need("varias", proposal_briefing.AREAS),
              "como_hoje": text, "equipe": text, "prazo": text, "email": text,
              "gargalos": text, "pedidos": text}
    return {
        "proposta_atendimento": company,
        "proposta_clinica": clinic,
        "agendamento": {"nome": text, "email": text, "assunto": text,
                        "horario": Need("uma", from_message=True)},
        "dados_contrato": {key: text for key in (
            "razao_social", "documento", "cep", "endereco", "bairro", "cidade", "uf",
            "assinante_nome", "assinante_cpf", "assinante_email", "assinante_cargo")},
    }


def _dir() -> Path:
    return Path(settings.content_dir) / "forms"


class FormError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _flow_ids() -> dict[str, str | None]:
    path = _dir() / FLOWS_FILE
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise FormError([f"forms/{FLOWS_FILE} não é TOML válido: {exc}"]) from None
    found: dict[str, str | None] = {}
    problems = []
    for name, entry in raw.items():
        value = entry.get("flow_id", "") if isinstance(entry, dict) else None
        if not isinstance(value, str):
            problems.append(f"forms/{FLOWS_FILE}: [{name}] flow_id precisa ser texto.")
            continue
        found[name] = value.strip() or None
    seen: dict[str, list[str]] = {}
    for name, flow_id in found.items():
        if flow_id:
            seen.setdefault(flow_id, []).append(name)
    for flow_id, names in seen.items():
        if len(names) > 1:
            problems.append(f"forms/{FLOWS_FILE}: o flow_id {flow_id} está em "
                            f"{' e '.join(f'[{n}]' for n in names)}; cada Flow tem o seu.")
    if problems:
        raise FormError(problems)
    return found


def _components(items, found: list[dict], where: str) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        raise FormError([f"{where}: \"children\" precisa ser uma lista."])
    for item in items:
        if not isinstance(item, dict):
            raise FormError([f"{where}: um componente não é um objeto."])
        found.append(item)
        _components(item.get("children"), found, where)


def _field_of(component: dict, screen_data: dict, where: str) -> Field:
    name = component.get("name")
    source = component.get("data-source")
    options: tuple[str, ...] | None = ()
    if isinstance(source, list):
        ids = []
        for option in source:
            if not isinstance(option, dict) or not isinstance(option.get("id"), str):
                raise FormError([f"{where}: \"{name}\" tem uma opção sem id."])
            ids.append(option["id"])
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            raise FormError([f"{where}: \"{name}\" repete o id {', '.join(repeated)}."])
        options = tuple(ids)
    elif isinstance(source, str):
        match = _DATA.fullmatch(source.strip())
        declared = screen_data.get(match.group(1)) if match else None
        if not isinstance(declared, dict) or declared.get("type") != "array":
            raise FormError([f"{where}: \"{name}\" usa a lista {source}, que a tela não "
                             f"declara como array em \"data\"."])
        options = None
    elif source is not None:
        raise FormError([f"{where}: \"{name}\" tem um data-source que não é lista nem "
                         f"referência."])
    return Field(kind=_KINDS[str(component["type"])], options=options)


def _parse(form_id: str, flow_id: str | None, data: object) -> Form:
    where = f"forms/{form_id}.json"
    if not isinstance(data, dict) or not isinstance(data.get("screens"), list) \
            or not data["screens"]:
        raise FormError([f"{where}: não tem telas."])
    screens: dict[str, tuple[dict, dict[str, dict]]] = {}
    completes: list[tuple[str, dict]] = []
    for screen in data["screens"]:
        if not isinstance(screen, dict) or not isinstance(screen.get("id"), str):
            raise FormError([f"{where}: uma tela não tem id."])
        layout = screen.get("layout")
        screen_data = screen.get("data", {})
        if not isinstance(layout, dict) or not isinstance(screen_data, dict):
            raise FormError([f"{where}: a tela {screen['id']} não tem layout ou data válidos."])
        found: list[dict] = []
        _components(layout.get("children"), found, where)
        named = {c["name"]: c for c in found
                 if isinstance(c.get("name"), str) and c.get("type") in _KINDS}
        screens[screen["id"]] = (screen_data, named)
        for component in found:
            action = component.get("on-click-action")
            if component.get("type") == "Footer" and isinstance(action, dict) \
                    and action.get("name") == "complete":
                completes.append((screen["id"], action))
    if not completes:
        raise FormError([f"{where}: nenhum botão com a ação complete."])
    last_screen, action = completes[-1]
    payload = action.get("payload") or {}
    if not isinstance(payload, dict):
        raise FormError([f"{where}: o payload da ação complete precisa ser um objeto."])
    fields: dict[str, Field] = {}
    unresolved: dict[str, str] = {}
    for key, reference in payload.items():
        text = str(reference).strip()
        same, other = _SAME_SCREEN.fullmatch(text), _OTHER_SCREEN.fullmatch(text)
        screen_id, name = (last_screen, same.group(1)) if same else \
            (other.group(1), other.group(2)) if other else ("", "")
        owner = screens.get(screen_id)
        if owner is None or name not in owner[1]:
            unresolved[key] = text
            continue
        fields[key] = _field_of(owner[1][name], owner[0], where)
    entry = data["screens"][0]
    return Form(id=form_id, flow_id=flow_id, screen=entry["id"], fields=fields,
                unresolved=unresolved, prefill=tuple(entry.get("data", {}).keys()))


def _read(form_id: str) -> Form | None:
    path = _dir() / f"{form_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FormError([f"forms/{form_id}.json não é JSON válido: {exc}"]) from None
    return _parse(form_id, _flow_ids().get(form_id), data)


@lru_cache(maxsize=None)
def load(form_id: str) -> Form | None:
    """The Flow's contract, or None when this organization has no such Flow."""
    return _read(form_id)


def _check(form: Form, needs: dict[str, Need]) -> list[str]:
    problems = []
    where = f"forms/{form.id}.json"
    for key, need in needs.items():
        field = form.fields.get(key)
        if field is None:
            if key in form.unresolved:
                problems.append(f"{where}: o campo \"{key}\" vale {form.unresolved[key]}, que "
                                f"não é um componente da tela (use ${{form.{key}}}).")
            else:
                problems.append(f"{where}: o payload não traz o campo \"{key}\".")
            continue
        if field.kind != need.kind:
            problems.append(f"{where}: \"{key}\" é respondido como {field.kind}, o código "
                            f"espera {need.kind}.")
        if need.from_message:
            if field.options is not None:
                problems.append(f"{where}: \"{key}\" precisa de uma lista que venha da "
                                f"mensagem (${{data.…}}), não de opções escritas no Flow.")
        elif need.ids:
            if field.options is None:
                problems.append(f"{where}: \"{key}\" precisa de opções fixas no Flow.")
                continue
            unknown = sorted(set(field.options) - set(need.ids))
            if unknown:
                problems.append(f"{where}: \"{key}\" tem opções que o código não conhece: "
                                f"{', '.join(unknown)}. Aceitas: {', '.join(need.ids)}.")
    return problems


def problems() -> list[str]:
    """What is wrong with the Flows, for the deploy check. Empty when absent."""
    found: list[str] = []
    try:
        ids = _flow_ids()
    except FormError as exc:
        return exc.problems
    for form_id, needs in required().items():
        try:
            form = _read(form_id)
        except FormError as exc:
            found.extend(exc.problems)
            continue
        if form is None:
            if ids.get(form_id):
                found.append(f"forms/{FLOWS_FILE}: [{form_id}] tem flow_id, mas não há "
                             f"forms/{form_id}.json.")
            continue
        found.extend(_check(form, needs))
    return found


def reset() -> None:
    load.cache_clear()
