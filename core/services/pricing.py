"""What a project costs, from the organization's own table (content/pricing.toml).

The model never prices anything: the desk collects what the customer can tell,
the rules here turn it into a scope, and the table turns the scope into a quote.
The table is optional — without it the desk does not quote at all — and a broken
one stops the deployment (content.validate, run by cli.load_content), with every
broken key named.

Money in the file is in reais, for whoever edits it; in here it is whole cents.
Factors and percentages stay Decimal. Rounding is half-up, always to the
multiple the table names.
"""
from __future__ import annotations

import hashlib
import tomllib
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path

from core.config import settings

FILE = "pricing.toml"
_LEVELS = ("basico", "intermediario_rag", "multi_agente")
_DOCS = ("nenhum", "pequeno", "grande")
_INFRA = ("compartilhada", "dedicada_lgpd", "vpc_cliente")
_PROCESS = ("mapeado", "nao_mapeado")
_BANDS = ("ate_500", "ate_2000", "ate_5000", "mais_5000")


class PricingError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Table:
    base: dict[str, int]
    por_canal: int
    integracao_simples: int
    integracao_complexa: int
    documentos: dict[str, int]
    arred_implantacao: int
    infra: dict[str, Decimal]
    processo: dict[str, Decimal]
    faixa_abaixo: Decimal
    faixa_acima: Decimal
    minimo_sustentacao: int
    sustentacao_pct: Decimal
    minimo_trafego: int
    por_conversa: Decimal
    por_usuario: int
    sla: int
    arred_mensalidade: int
    descontos_implantacao: tuple[Decimal, ...]
    parcelas_max: int
    desconto_fidelidade: Decimal
    piso_implantacao: int
    piso_mensalidade: int
    validade_dias: int
    padrao_usuarios: int
    padrao_documentos: str
    padrao_sla: bool
    volume_faixas: dict[str, int]
    aliases: dict[str, str]
    digest: str
    # The card fee of the Asaas account, added to a setup paid by card in
    # installments: a share (0.0249) and a fixed part, in cents.
    taxa_cartao_pct: Decimal = Decimal(0)
    taxa_cartao_fixa: int = 0


def normalize(text: str) -> str:
    """Lowercase, no accents, single spaces: how names are compared."""
    plain = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return " ".join(plain.lower().split())


class _Reader:
    """Reads keys from the raw TOML, noting every problem instead of stopping."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.problems: list[str] = []

    def _get(self, path: str):
        node = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                self.problems.append(f"pricing.toml: falta {path}.")
                return None
            node = node[part]
        return node

    def _number(self, path: str, value) -> Decimal | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.problems.append(f"pricing.toml: {path} precisa ser um número.")
            return None
        amount = Decimal(str(value))
        if not amount.is_finite():  # TOML accepts nan and inf
            self.problems.append(f"pricing.toml: {path} precisa ser um número finito.")
            return None
        return amount

    def money(self, path: str, value=None, *, zero_ok: bool = False) -> int:
        value = self._get(path) if value is None else value
        if value is None:
            return 0
        amount = self._number(path, value)
        if amount is None:
            return 0
        cents = int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if amount < 0 or (cents == 0 and not zero_ok):
            self.problems.append(f"pricing.toml: {path} precisa ser de pelo menos R$ 0,01.")
        return cents

    def cents_rate(self, path: str) -> Decimal:
        """A per-unit price that may have fractions of a cent."""
        amount = self._number(path, self._get(path)) if self._get(path) is not None else None
        if amount is None:
            return Decimal(0)
        if amount <= 0:
            self.problems.append(f"pricing.toml: {path} precisa ser maior que zero.")
        return amount * 100

    def factor(self, path: str, value) -> Decimal:
        amount = self._number(path, value)
        if amount is None:
            return Decimal(1)
        if amount < 1:
            self.problems.append(f"pricing.toml: {path} precisa ser 1 ou mais (é um multiplicador).")
        return amount

    def percent(self, path: str, value=None) -> Decimal:
        value = self._get(path) if value is None else value
        if value is None:
            return Decimal(0)
        amount = self._number(path, value)
        if amount is None:
            return Decimal(0)
        if not Decimal(0) <= amount < Decimal(1):
            self.problems.append(f"pricing.toml: {path} precisa estar entre 0 e 1 (ex.: 0.10 = 10%).")
        return amount

    def whole(self, path: str, value=None) -> int:
        value = self._get(path) if value is None else value
        if value is None:
            return 1
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            self.problems.append(f"pricing.toml: {path} precisa ser um número inteiro de 1 ou mais.")
            return 1
        return value

    def choices(self, path: str, keys: tuple[str, ...]) -> dict:
        node = self._get(path)
        if node is None:
            return {}
        if not isinstance(node, dict):
            self.problems.append(f"pricing.toml: {path} precisa ser uma tabela.")
            return {}
        for key in keys:
            if key not in node:
                self.problems.append(f"pricing.toml: falta {path}.{key}.")
        return node


def parse_table(raw: dict, digest: str) -> Table:
    r = _Reader(raw)
    base = {k: r.money(f"implantacao.base.{k}", v)
            for k, v in r.choices("implantacao.base", _LEVELS).items()}
    docs = {k: r.money(f"implantacao.documentos.{k}", v, zero_ok=(k == "nenhum"))
            for k, v in r.choices("implantacao.documentos", _DOCS).items()}
    infra = {k: r.factor(f"implantacao.fatores.infra.{k}", v)
             for k, v in r.choices("implantacao.fatores.infra", _INFRA).items()}
    process = {k: r.factor(f"implantacao.fatores.processo.{k}", v)
               for k, v in r.choices("implantacao.fatores.processo", _PROCESS).items()}
    bands = {k: r.whole(f"padroes.volume.{k}", v)
             for k, v in r.choices("padroes.volume", _BANDS).items()}

    discounts_raw = r._get("negociacao.descontos_implantacao")
    discounts: tuple[Decimal, ...] = ()
    if discounts_raw is not None:
        if not isinstance(discounts_raw, list) or not discounts_raw:
            r.problems.append("pricing.toml: negociacao.descontos_implantacao precisa ser uma lista.")
        else:
            discounts = tuple(r.percent(f"negociacao.descontos_implantacao[{i}]", v)
                              for i, v in enumerate(discounts_raw))
            if list(discounts) != sorted(discounts):
                r.problems.append("pricing.toml: negociacao.descontos_implantacao precisa estar em ordem crescente.")

    documents_default = r._get("padroes.documentos")
    if documents_default is not None and documents_default not in _DOCS:
        r.problems.append(f"pricing.toml: padroes.documentos precisa ser um de {', '.join(_DOCS)}.")
    sla_default = r._get("padroes.sla")
    if sla_default is not None and not isinstance(sla_default, bool):
        r.problems.append("pricing.toml: padroes.sla precisa ser true ou false.")

    aliases: dict[str, str] = {}
    systems = r._get("sistemas")
    if systems is not None and not isinstance(systems, dict):
        r.problems.append("pricing.toml: sistemas precisa ser uma tabela (nome = [apelidos]).")
    if isinstance(systems, dict):
        for canonical, names in systems.items():
            if not isinstance(names, list) or not names:
                r.problems.append(f"pricing.toml: sistemas.{canonical} precisa ser uma lista de nomes.")
                continue
            for name in names:
                if not isinstance(name, str) or not normalize(name):
                    r.problems.append(f"pricing.toml: sistemas.{canonical} tem um nome vazio ou que não é texto.")
                    continue
                key = normalize(name)
                if key in aliases and aliases[key] != canonical:
                    r.problems.append(f"pricing.toml: o nome '{key}' está em sistemas.{aliases[key]} "
                                      f"e em sistemas.{canonical}.")
                aliases[key] = canonical

    table = Table(
        base=base,
        por_canal=r.money("implantacao.por_canal"),
        integracao_simples=r.money("implantacao.integracao_simples"),
        integracao_complexa=r.money("implantacao.integracao_complexa"),
        documentos=docs,
        arred_implantacao=r.money("implantacao.arredondar"),
        infra=infra,
        processo=process,
        faixa_abaixo=r.percent("implantacao.faixa.abaixo"),
        faixa_acima=r.percent("implantacao.faixa.acima"),
        minimo_sustentacao=r.money("mensalidade.minimo_sustentacao"),
        sustentacao_pct=r.percent("mensalidade.sustentacao_pct"),
        minimo_trafego=r.money("mensalidade.minimo_trafego"),
        por_conversa=r.cents_rate("mensalidade.por_conversa"),
        por_usuario=r.money("mensalidade.por_usuario"),
        sla=r.money("mensalidade.sla"),
        arred_mensalidade=r.money("mensalidade.arredondar"),
        descontos_implantacao=discounts,
        parcelas_max=r.whole("negociacao.parcelas_max"),
        desconto_fidelidade=r.percent("negociacao.desconto_fidelidade"),
        piso_implantacao=r.money("negociacao.piso_implantacao"),
        piso_mensalidade=r.money("negociacao.piso_mensalidade"),
        validade_dias=r.whole("negociacao.validade_dias"),
        padrao_usuarios=r.whole("padroes.usuarios_painel"),
        padrao_documentos=documents_default if documents_default in _DOCS else "nenhum",
        padrao_sla=sla_default if isinstance(sla_default, bool) else False,
        volume_faixas=bands,
        aliases=aliases,
        digest=digest,
        taxa_cartao_pct=r.percent("negociacao.taxa_cartao_pct"),
        taxa_cartao_fixa=r.money("negociacao.taxa_cartao_fixa", zero_ok=True),
    )
    if r.problems:
        raise PricingError(r.problems)
    return table


def _path() -> Path:
    return Path(settings.content_dir) / FILE


def _read() -> tuple[dict, str] | None:
    path = _path()
    if not path.is_file():
        return None
    data = path.read_bytes()
    return tomllib.loads(data.decode("utf-8")), hashlib.sha256(data).hexdigest()[:12]


@lru_cache(maxsize=1)
def load_table() -> Table | None:
    """The table, or None when this organization has none (the desk does not quote)."""
    found = _read()
    return None if found is None else parse_table(*found)


def problems() -> list[str]:
    """What is wrong with the table, for the deploy check. Empty when absent."""
    try:
        found = _read()
    except tomllib.TOMLDecodeError as exc:
        return [f"pricing.toml não é TOML válido: {exc}"]
    except UnicodeDecodeError:
        return ["pricing.toml não é TOML válido: o arquivo não está em UTF-8."]
    if found is None:
        return []
    try:
        parse_table(*found)
    except PricingError as exc:
        return exc.problems
    return []


def reset() -> None:
    load_table.cache_clear()


# ── From what the customer said to a scope ──────────────────────────────────

AREAS = ("atendimento", "vendas", "financeiro", "documentos_juridico", "suporte_tecnico", "rh")
REQUIRED = ("canais", "sistemas", "volume", "areas", "roteiro")
# Words that make any system an integration with no ready API, whatever its name.
_HARD_WORDS = ("sem api", "proprio", "antigo", "banco local")


@dataclass(frozen=True)
class System:
    name: str
    sem_api: bool = False


@dataclass(frozen=True)
class Answers:
    """What the customer told the desk. None = not said; an empty tuple = "none"."""

    canais: tuple[str, ...] | None = None
    sistemas: tuple[System, ...] | None = None
    areas: tuple[str, ...] | None = None
    volume: int | None = None
    volume_faixa: str | None = None
    roteiro_definido: bool | None = None
    documentos: str | None = None
    dado_sensivel: bool | None = None
    nuvem_propria: bool | None = None
    sla: bool | None = None
    usuarios: int | None = None


@dataclass(frozen=True)
class Scope:
    complexidade: str
    canais: int
    simples: int
    complexas: int
    documentos: str
    infra: str
    processo: str
    conversas: int
    usuarios: int
    sla: bool
    premissas: tuple[str, ...]


def missing(answers: Answers) -> list[str]:
    """What must still be asked before a quote, in the order it is asked."""
    have = {
        "canais": answers.canais is not None,
        "sistemas": answers.sistemas is not None,
        "volume": answers.volume is not None or answers.volume_faixa is not None,
        "areas": answers.areas is not None,
        "roteiro": answers.roteiro_definido is not None,
    }
    return [item for item in REQUIRED if not have[item]]


def _identity(name: str, table: Table) -> str:
    """The same system however it was called: "Outlook antigo, sem API" is Outlook."""
    core = name
    for word in _HARD_WORDS:
        core = core.replace(word, " ")
    core = normalize(core.replace(",", " ").replace("-", " "))
    return table.aliases.get(core) or table.aliases.get(name) or core or name


def _integrations(systems: tuple[System, ...], table: Table) -> tuple[int, int]:
    """(simple, hard), one per system; a system said once to have no API is hard."""
    kinds: dict[str, str] = {}
    for system in systems:
        name = normalize(system.name)
        if not name:
            continue
        identity = _identity(name, table)
        known = identity in table.aliases.values()
        hard = system.sem_api or any(word in name for word in _HARD_WORDS) or not known
        if hard or identity not in kinds:
            kinds[identity] = "hard" if hard else "simple"
    values = list(kinds.values())
    return values.count("simple"), values.count("hard")


def derive(answers: Answers, table: Table) -> Scope:
    """The scope the table prices, by fixed rules (see the spec's Parte 1)."""
    gaps = missing(answers)
    if gaps:
        raise ValueError(f"Faltam respostas para cotar: {', '.join(gaps)}.")
    assert answers.canais is not None and answers.sistemas is not None
    assert answers.areas is not None and answers.roteiro_definido is not None
    areas = {normalize(a) for a in answers.areas}
    unknown = areas - set(AREAS)
    if unknown:
        raise ValueError(f"área desconhecida: {', '.join(sorted(unknown))}.")
    if answers.volume is not None:
        conversations = answers.volume
    elif answers.volume_faixa in table.volume_faixas:
        conversations = table.volume_faixas[answers.volume_faixa]
    else:
        raise ValueError(f"faixa de volume desconhecida: {answers.volume_faixa}.")

    assumed: list[str] = []
    users = answers.usuarios
    if users is None:
        users = table.padrao_usuarios
        assumed.append("usuarios")
    documents = answers.documentos
    if documents is None:
        documents = table.padrao_documentos
        assumed.append("documentos")
    if documents not in table.documentos:
        raise ValueError(f"volume de documentos desconhecido: {documents}.")
    sla = answers.sla
    if sla is None:
        sla = table.padrao_sla
        assumed.append("sla")

    if len(areas) >= 2:
        level = "multi_agente"
    elif documents != "nenhum":
        level = "intermediario_rag"
    else:
        level = "basico"
    if answers.nuvem_propria:
        infra = "vpc_cliente"
    elif answers.dado_sensivel:
        infra = "dedicada_lgpd"
    else:
        infra = "compartilhada"
    simple, hard = _integrations(answers.sistemas, table)
    return Scope(
        complexidade=level,
        canais=len({normalize(c) for c in answers.canais if normalize(c)}),
        simples=simple,
        complexas=hard,
        documentos=documents,
        infra=infra,
        processo="mapeado" if answers.roteiro_definido else "nao_mapeado",
        conversas=conversations,
        usuarios=users,
        sla=bool(sla),
        premissas=tuple(assumed),
    )


# ── The quote ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Quote:
    setup_cents: int
    setup_low_cents: int
    setup_high_cents: int
    monthly_cents: int
    table_digest: str
    # For the team only — base, factors, the parts of the monthly fee. Never
    # rendered to the customer.
    detail: dict[str, int | str]


def round_to(cents: Decimal, step_cents: int) -> int:
    """Half-up to the nearest multiple of `step_cents`."""
    steps = (cents / step_cents).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(steps) * step_cents


def quote(scope: Scope, table: Table) -> Quote:
    """The spec's Parte 2, in its order. Every coefficient comes from the table."""
    base = table.base[scope.complexidade]
    channels_and_integrations = (scope.canais * table.por_canal
                                 + scope.simples * table.integracao_simples
                                 + scope.complexas * table.integracao_complexa)
    documents = table.documentos[scope.documentos]
    factor = table.infra[scope.infra] * table.processo[scope.processo]
    subtotal = Decimal(base + channels_and_integrations + documents)
    setup = round_to(subtotal * factor, table.arred_implantacao)
    low = round_to(setup * (1 - table.faixa_abaixo), table.arred_implantacao)
    high = round_to(setup * (1 + table.faixa_acima), table.arred_implantacao)

    upkeep = max(Decimal(table.minimo_sustentacao), table.sustentacao_pct * setup)
    traffic = max(Decimal(table.minimo_trafego), scope.conversas * table.por_conversa)
    users = scope.usuarios * table.por_usuario
    sla = table.sla if scope.sla else 0
    monthly = round_to(upkeep + traffic + users + sla, table.arred_mensalidade)
    return Quote(
        setup_cents=setup,
        setup_low_cents=low,
        setup_high_cents=high,
        monthly_cents=monthly,
        table_digest=table.digest,
        detail={
            "base": base,
            "canais_e_integracoes": channels_and_integrations,
            "documentos": documents,
            "fator": str(factor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "sustentacao": int(upkeep.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
            "trafego": int(traffic.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
            "usuarios": users,
            "sla": sla,
        },
    )



# How the setup is paid (proposal_quotes.forma_implantacao).
SETUP_TERMS = ("metade", "parcelado")


@dataclass(frozen=True)
class SetupPlan:
    """The setup's payments: the first, when the contract is signed, then
    `count - 1` more of `each`. "metade": the second when the setup is
    accepted; "parcelado": monthly."""
    forma: str
    count: int
    first: int
    each: int


def setup_plan(total_cents: int, forma: str, parcelas: int) -> SetupPlan:
    """Equal payments, the cents left over going in the first (as in the
    model: R$ 1.916,70 and 5 of 1.916,66). Never more payments than cents."""
    count = 2 if forma == "metade" else max(parcelas, 1)
    count = max(1, min(count, total_cents))
    each = total_cents // count
    return SetupPlan(forma, count, total_cents - each * (count - 1), each)


def with_card_fee(net_cents: int, table: Table) -> int:
    """What a card payment must be so that, after the card fee, exactly
    `net_cents` reaches the Converge (rounded up to the cent)."""
    gross = (Decimal(net_cents) + table.taxa_cartao_fixa) / (1 - table.taxa_cartao_pct)
    return int(gross.to_integral_value(rounding=ROUND_CEILING))
