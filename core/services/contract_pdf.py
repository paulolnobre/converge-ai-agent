"""The contract PDF, made from the model in
docs/modelos/proposta-contrato-implantacao.html: the fixed text (the body and
Anexo II, in content/contrato/), the parties from the data the customer sent
in the Flow `dados_contrato`, the Anexo I from the request and its quote, and
the Anexo III from the quote and the price table.

What the person typed is escaped: it is shown as written, never read as layout.
The text is still under the lawyer's review; while it is, the PDF carries the
stamp in content/messages.toml (contract_pdf.selo).
"""
from __future__ import annotations

import io
import logging
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from core import channels, content
from core.models import Contact, ProposalBriefing, ProposalQuote
from core.repositories import contacts as contacts_repo
from core.repositories import contract_details as details_repo
from core.repositories import proposal_briefings as repo
from core.repositories import proposal_quotes as quotes_repo
from core.services import pricing, proposal_pdf, proposal_quote
from core.services.proposal_pdf import INK, INK_2, LINE, SOFT, YELLOW, NoQuote, brl

logger = logging.getLogger(__name__)

_FOLDER = "contrato"
_LIST = ", "
_MONTHS = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
           "setembro", "outubro", "novembro", "dezembro")
_FIELDS = ("funcao", "aceite", "finalidade")
# Provider names only: a generic word ("planilha") names no provider and is kept
# as the customer wrote it.
_GOOGLE = ("google", "gmail")
_MICROSOFT = ("microsoft", "outlook", "office", "sharepoint", "onedrive", "excel")


class NoDetails(Exception):
    """The customer has not sent the contract's data yet."""


@dataclass(frozen=True)
class Function:
    area: str
    funcao: str
    aceite: str
    finalidade: str


@dataclass(frozen=True)
class Data:
    empresa: str
    contratante: str
    assinante: str
    hoje: date
    clinica: bool
    ficha: tuple[tuple[str, str], ...]
    funcoes: tuple[Function, ...]
    tratamento: tuple[tuple[str, str], ...]
    comercial: tuple[tuple[str, str], ...]


# ── The fixed text ───────────────────────────────────────────────────────────

def _root() -> Path:
    return content._root() / _FOLDER


def fixed_text(name: str) -> list[str]:
    """The lines of corpo.txt or anexo_ii.txt, without comments or blanks."""
    lines = (_root() / name).read_text(encoding="utf-8").splitlines()
    return [line.rstrip() for line in lines if line.strip() and not line.startswith("# ")]


@lru_cache(maxsize=1)
def functions() -> dict[str, dict[str, str]]:
    with (_root() / "funcoes.toml").open("rb") as handle:
        return tomllib.load(handle)


def reset() -> None:
    functions.cache_clear()


# Every area the questionnaires offer (content/messages.toml [proposal]).
AREA_KEYS = (*(f"biz_area_{k}" for k in ("atendimento", "vendas", "financeiro",
                                          "documentos_juridico", "suporte_tecnico", "rh")),
             *(f"area_{k}" for k in ("agenda", "triagem", "prontuario", "receitas",
                                     "pagamentos", "financeiro")))


def problems(area_labels: list[str] | None = None) -> list[str]:
    """What would leave a contract without its text, checked at deploy."""
    if area_labels is None:
        area_labels = []
        for key in AREA_KEYS:
            try:
                area_labels.append(content.text(f"proposal.{key}"))
            except content.ContentError:
                continue  # the missing message is reported by content.validate
        area_labels = list(dict.fromkeys(area_labels))
    found: list[str] = []
    for name in ("corpo.txt", "anexo_ii.txt"):
        try:
            if not fixed_text(name):
                found.append(f"content/{_FOLDER}/{name} is empty.")
        except FileNotFoundError:
            found.append(f"content/{_FOLDER}/{name} is missing.")
    try:
        table = functions()
    except (FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        return [*found, f"content/{_FOLDER}/funcoes.toml: {exc}"]
    for label in area_labels:
        entry = table.get(label)
        if not isinstance(entry, dict):
            found.append(f"content/{_FOLDER}/funcoes.toml has no [\"{label}\"].")
            continue
        for key in _FIELDS:
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                found.append(f"content/{_FOLDER}/funcoes.toml [\"{label}\"] needs {key}.")
    return found


# ── The data ─────────────────────────────────────────────────────────────────

def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def document(value: str) -> tuple[str, str]:
    """("CNPJ", "11.222.333/0001-81") or ("CPF", "529.982.247-25")."""
    d = _digits(value)
    if len(d) == 14:
        return "CNPJ", f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return "CPF", f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return "CNPJ", value


def _cep(value: str) -> str:
    d = _digits(value)
    return f"{d[:5]}-{d[5:]}" if len(d) == 8 else value


def phone(contact: Contact) -> str:
    """"(91) 99999-0000" from the contact's phone or WhatsApp number; "" if none."""
    raw = contact.phone or ""
    if not raw and channels.is_whatsapp(contact.chat_id or ""):
        raw = (contact.chat_id or "").removeprefix("wa:")
    d = _digits(raw)
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return raw


def long_date(day: date) -> str:
    return f"{day.day:02d} de {_MONTHS[day.month - 1]} de {day.year}"


def _items(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(_LIST) if item.strip()]


def _and(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} e {items[-1]}"


def _t(key: str) -> str:
    return content.text(f"contract_pdf.{key}")


def _r(key: str, **values) -> str:
    return content.render(f"contract_pdf.{key}", **values)


def _functions(b: ProposalBriefing, quote: ProposalQuote) -> tuple[Function, ...]:
    """The areas the quote priced, with their contract text. An area typed by
    hand (not priced) is the team's to add, as in the proposal."""
    unpriced = {p.removeprefix(proposal_quote.UNPRICED_AREA) for p in quote.premises
                if p.startswith(proposal_quote.UNPRICED_AREA)}
    table = functions()
    found: list[Function] = []
    for area in _items(b.answers.get("areas", "")):
        if area in unpriced:
            continue
        entry = table.get(area)
        if not isinstance(entry, dict):
            logger.error("No contract text for the area %r", area)
            continue
        found.append(Function(area, *(str(entry.get(key, "")).strip() for key in _FIELDS)))
    return tuple(found)


def _names(tool: str, words: tuple[str, ...]) -> bool:
    """Whole words only: "LibreOffice" is not "Office"."""
    return any(re.search(rf"\b{word}\b", tool.lower()) for word in words)


def _third_parties(channel_names: list[str], tools: list[str]) -> str:
    """The base suboperators, Meta for its channels, and each integration the
    customer named: the known ones with their role, any other as contracted by
    the Contratante. Never a provider the customer did not name."""
    parts = [_t("terceiros_base")]
    lowered = " ".join(channel_names).lower()
    if "whatsapp" in lowered or "instagram" in lowered:
        parts.append(_t("terceiros_meta"))
    known = {"google": False, "microsoft": False, "asaas": False}
    for tool in tools:
        if _names(tool, _GOOGLE):
            known["google"] = True
        elif _names(tool, _MICROSOFT):
            known["microsoft"] = True
        elif _names(tool, ("asaas",)):
            known["asaas"] = True
        else:
            parts.append(_r("terceiros_outro", nome=tool))
    parts += [_t(f"terceiros_{key}") for key, used in known.items() if used]
    return "; ".join(parts)


def assemble(b: ProposalBriefing, contact: Contact, quote: ProposalQuote,
             table: pricing.Table, details: dict[str, str], today: date) -> Data:
    a = b.answers
    clinic = a.get("trilha") == "clinica"
    empresa = details.get("razao_social", "")
    signer = details.get("assinante_nome", "")
    kind, number = document(details.get("documento", ""))
    address = (f"{details.get('endereco', '')}, {details.get('bairro', '')}, "
               f"{details.get('cidade', '')}/{details.get('uf', '')}, "
               f"CEP {_cep(details.get('cep', ''))}")
    contratante = _r("contratante", nome=empresa, tipo=kind, documento=number,
                     endereco=address, assinante=signer,
                     cpf=document(details.get("assinante_cpf", ""))[1],
                     email=details.get("assinante_email", ""))

    funcs = _functions(b, quote)
    channel_names = _items(a.get("canais_lista", ""))
    tools = [x for x in _items(a.get("sistemas_lista", "")) if x.lower() != "nenhum"]
    scope = quote.scope
    conversations = int(scope.get("conversas") or 0)
    users = int(scope.get("usuarios") or 0)
    docs = str(scope.get("documentos") or "nenhum")
    sensitive = clinic or scope.get("infra") == "dedicada_lgpd"
    volume = _r("volume", n=proposal_pdf._number(conversations))
    ficha = (
        (_t("f_funcoes"), f"{'; '.join(f.area for f in funcs)} {_t('ver_funcoes')}".strip()),
        (_t("f_canais"), _and(channel_names)),
        (_t("f_integracoes"), "; ".join(tools) or _t("nenhuma_integracao")),
        (_t("f_documentos"), _t(f"documentos_{docs}") if docs in ("nenhum", "pequeno", "grande")
         else _t("documentos_nenhum")),
        (_t("f_volume"), volume),
        (_t("f_usuarios"), str(users)),
        (_t("f_sensiveis"), _t("sim") if sensitive else _t("nao")),
        (_t("f_ajustes"), _t("ajustes")),
        (_t("f_prazo"), _t("prazo")),
        (_t("f_suporte"), _t("suporte")),
    )

    who = ", ".join(x for x in (signer, phone(contact)) if x)
    responsible = _r("responsavel", contato=who) + (_t("responsavel_clinica") if clinic else "")
    tratamento = (
        (_t("d_finalidades"), _and([f.finalidade for f in funcs])),
        (_t("d_categorias"), _t("categorias_clinica" if clinic else "categorias_empresa")),
        (_t("d_responsavel"), responsible),
        (_t("d_retencao"), _t("retencao")),
        (_t("d_terceiros"), _third_parties(channel_names, tools)),
        (_t("d_transferencia"), _t("transferencia")),
    )

    setup = brl(quote.setup_cents)
    plan = pricing.setup_plan(quote.setup_cents, quote.forma_implantacao, table.parcelas_max)
    if plan.count == 1:
        setup_text = _r("implantacao_vista", valor=setup)
    elif plan.forma == "metade":
        setup_text = _r("implantacao_metade", valor=setup, primeira=brl(plan.first),
                        segunda=brl(plan.each))
    else:
        setup_text = _r("implantacao_parcelas", valor=setup, n=plan.count,
                        primeira=brl(plan.first), resto=plan.count - 1, parcela=brl(plan.each))
    sla_text = _r("sla_sim" if scope.get("sla") else "sla_nao", valor=brl(table.sla))
    pct = int((table.desconto_fidelidade * 100).to_integral_value())
    comercial = (
        (_t("c_implantacao"), setup_text),
        (_t("c_mensalidade"), _r("mensalidade", valor=brl(quote.monthly_cents),
                                 dia=_t("vencimento_dia"))),
        (_t("c_volume"), volume),
        (_t("c_excedente"), _r("excedente", valor=brl(Decimal(table.por_conversa)),
                               limite=_t("excedente_limite"))),
        (_t("c_usuario"), _r("usuario", valor=brl(table.por_usuario))),
        (_t("c_sla"), sla_text),
        (_t("c_fidelidade"), _r("fidelidade", pct=pct)),
        (_t("c_tarifas"), _t("tarifas")),
        (_t("c_validade"), _r("validade", dias=table.validade_dias)),
    )
    return Data(empresa=empresa or content.text("proposal_doc.no_company"),
                contratante=contratante, assinante=signer, hoje=today, clinica=clinic,
                ficha=ficha, funcoes=funcs, tratamento=tratamento, comercial=comercial)


def filename(data: Data) -> str:
    company = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", data.empresa).strip()[:80]
    return _r("arquivo", empresa=company or "cliente")


# ── The layout ───────────────────────────────────────────────────────────────

def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("c-base", fontName="Helvetica", fontSize=9, leading=12.6,
                          textColor=INK, spaceAfter=4)
    return {
        "base": base,
        "brand": ParagraphStyle("c-brand", parent=base, fontName="Helvetica-Bold",
                                fontSize=14, spaceAfter=0),
        "stamp": ParagraphStyle("c-stamp", parent=base, fontSize=8, textColor=INK_2,
                                alignment=2, spaceAfter=0),
        "title": ParagraphStyle("c-title", parent=base, fontName="Helvetica-Bold",
                                fontSize=15, leading=19, spaceBefore=6, spaceAfter=8),
        "h3": ParagraphStyle("c-h3", parent=base, fontName="Helvetica-Bold", fontSize=11,
                             leading=14, spaceBefore=8, spaceAfter=4),
        "h4": ParagraphStyle("c-h4", parent=base, fontName="Helvetica-Bold", fontSize=9.5,
                             spaceBefore=4, spaceAfter=3),
        "cell": ParagraphStyle("c-cell", parent=base, fontSize=8.5, leading=11.5,
                               spaceAfter=0),
        "term": ParagraphStyle("c-term", parent=base, fontName="Helvetica-Bold",
                               fontSize=8.5, leading=11.5, spaceAfter=0),
        "sign": ParagraphStyle("c-sign", parent=base, fontSize=8.5, leading=11.5,
                               textColor=INK_2, spaceAfter=0),
    }


def _inline(text: str) -> str:
    """Escaped, with **bold** kept."""
    parts = escape(text).split("**")
    return "".join(f"<b>{p}</b>" if i % 2 else p for i, p in enumerate(parts))


def _grid(rows: list[list[str]], s: dict[str, ParagraphStyle], *, header: bool,
          widths: list | None = None) -> Table:
    cells = [[Paragraph(_inline(c), s["term"] if (header and i == 0) or (not header and j == 0)
                        else s["cell"]) for j, c in enumerate(row)]
             for i, row in enumerate(rows)]
    grid = Table(cells, colWidths=widths, hAlign="LEFT", repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("GRID", (0, 0), (-1, -1), 0.5, LINE),
             ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    style.append(("BACKGROUND", (0, 0), (-1, 0) if header else (0, -1), SOFT))
    grid.setStyle(TableStyle(style))
    return grid


def render_text(lines: list[str], s: dict[str, ParagraphStyle]) -> list:
    """The fixed text: '## ' heading, '### ' subheading, '- ' item, '| ' table row."""
    story: list = []
    bullets: list[str] = []
    rows: list[list[str]] = []

    def flush() -> None:
        if bullets:
            items: list[Any] = [ListItem(Paragraph(_inline(b), s["base"]), leftIndent=12)
                                for b in bullets]
            story.append(ListFlowable(items, bulletType="bullet", start="•", leftIndent=12,
                                      bulletFontSize=8))
            bullets.clear()
        if rows:
            story.append(_grid([list(r) for r in rows], s, header=True))
            story.append(Spacer(1, 4))
            rows.clear()

    for line in lines:
        if line.startswith("- "):
            if rows:
                flush()
            bullets.append(line[2:])
            continue
        if line.startswith("|"):
            if bullets:
                flush()
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
            continue
        flush()
        if line.startswith("### "):
            story.append(Paragraph(_inline(line[4:]), s["h4"]))
        elif line.startswith("## "):
            story.append(Paragraph(_inline(line[3:]), s["h3"]))
        else:
            story.append(Paragraph(_inline(line), s["base"]))
    flush()
    return story


def _top(s: dict[str, ParagraphStyle]) -> Table:
    logo = Image(str(proposal_pdf._LOGO), width=9 * mm, height=9 * mm)
    stamp = _t("selo")
    row = Table([[logo, Paragraph("Converge", s["brand"]),
                  Paragraph(escape(stamp), s["stamp"]) if stamp else ""]],
                colWidths=[11 * mm, 40 * mm, None])
    row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("LINEBELOW", (0, 0), (-1, 0), 2, YELLOW),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return row


def _signatures(data: Data, s: dict[str, ParagraphStyle]) -> Table:
    def block(label: str, name: str) -> list:
        return [Spacer(1, 22), Paragraph("_" * 44, s["sign"]),
                Paragraph(f"<b>{escape(label)}</b>", s["cell"]),
                Paragraph(escape(name), s["sign"])]

    grid = Table([[block(_t("rotulo_converge"), _t("assinatura_converge")),
                   block(_t("rotulo_contratante"),
                         _r("assinatura_contratante", assinante=data.assinante,
                            nome=data.empresa))]],
                 colWidths=["50%", "50%"])
    grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return grid


def _story(data: Data) -> list:
    s = _styles()
    story: list = [_top(s), Paragraph(escape(_t("titulo")), s["title"]),
                   Paragraph(escape(_t("sec_partes")), s["h3"]),
                   Paragraph(escape(_t("contratada")), s["base"]),
                   Paragraph(escape(data.contratante), s["base"])]
    story += render_text(fixed_text("corpo.txt"), s)
    story.append(KeepTogether([
        Spacer(1, 6), Paragraph(escape(_r("local_data", data=long_date(data.hoje))), s["base"]),
        _signatures(data, s)]))

    story += [PageBreak(), Paragraph(escape(_t("anexo_i")), s["h3"]),
              Paragraph(escape(_t("ficha")), s["h4"]),
              _grid([list(r) for r in data.ficha], s, header=False, widths=[55 * mm, None]),
              Spacer(1, 4), Paragraph(escape(_t("funcoes_titulo")), s["h4"])]
    items: list[Any] = [
        ListItem(Paragraph(f"<b>{escape(f.area)}</b>: {escape(f.funcao)}", s["base"]),
                 leftIndent=12)
        for f in data.funcoes]
    if items:
        story.append(ListFlowable(items, bulletType="bullet", start="•", leftIndent=12,
                                  bulletFontSize=8))
    story.append(Paragraph(escape(_t("nao_incluido")), s["base"]))
    story.append(Paragraph(escape(_r("aceite", lista="; ".join(
        f.aceite for f in data.funcoes))), s["base"]))
    story += [Paragraph(escape(_t("dados_titulo")), s["h4"]),
              _grid([list(r) for r in data.tratamento], s, header=False,
                    widths=[55 * mm, None])]

    story += [PageBreak(), *render_text(fixed_text("anexo_ii.txt"), s)]
    story.append(KeepTogether([
        Spacer(1, 6), Paragraph(escape(_t("anexo_iii")), s["h3"]),
        _grid([list(r) for r in data.comercial], s, header=False, widths=[55 * mm, None])]))
    return story


def _footer(canvas, doc) -> None:
    canvas.saveState()
    width, _ = doc.pagesize
    canvas.setStrokeColor(LINE)
    canvas.line(doc.leftMargin, 14 * mm, width - doc.rightMargin, 14 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(INK_2)
    canvas.drawString(doc.leftMargin, 10 * mm, _t("rodape"))
    canvas.drawRightString(width - doc.rightMargin, 10 * mm,
                           _r("pagina", pagina=canvas.getPageNumber()))
    canvas.restoreState()


def build(data: Data) -> bytes:
    from reportlab.lib.pagesizes import A4

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=14 * mm, bottomMargin=20 * mm,
                            title=filename(data).removesuffix(".pdf"), author="Converge")
    doc.build(_story(data), onFirstPage=_footer, onLaterPages=_footer)
    return out.getvalue()


async def generate(conn, briefing_id: int, *, today: date | None = None) -> tuple[bytes, str]:
    """The contract of a request and its file name. LookupError when there is
    no such request; NoQuote without a quote (or price table); NoDetails before
    the customer sends the contract's data."""
    pdf, name, _ = await generate_with_terms(conn, briefing_id, today=today)
    return pdf, name


async def generate_with_terms(conn, briefing_id: int, *, today: date | None = None
                              ) -> tuple[bytes, str, pricing.SetupPlan]:
    """The same, with the setup's payments this contract states: what the
    signed contract is charged by (contract_charge), whatever is edited later."""
    b = await repo.get(conn, briefing_id)
    if b is None:
        raise LookupError(briefing_id)
    quote = await quotes_repo.get(conn, briefing_id)
    table = pricing.load_table()
    if quote is None or table is None:
        raise NoQuote(briefing_id)
    details = await details_repo.get(conn, briefing_id)
    if not details:
        raise NoDetails(briefing_id)
    contact = await contacts_repo.get_by_id(conn, b.contact_id)
    data = assemble(b, contact or Contact(id=b.contact_id), quote, table, details,
                    today or proposal_pdf._today())
    plan = pricing.setup_plan(quote.setup_cents, quote.forma_implantacao, table.parcelas_max)
    return build(data), filename(data), plan
