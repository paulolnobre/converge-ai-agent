"""The proposal PDF, made from the model the owner approved
(docs/modelos/proposta-contrato-implantacao.html): the request's answers and
its preliminary quote, as the team left it, in the Converge layout.

The texts are in content/messages.toml ([proposal_pdf]); the prices that are
not the quote's (installments, the loyalty discount, an extra conversation or
user) come from content/pricing.toml. What the person typed is escaped: it is
shown as written, never read as layout.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from core import content
from core.config import settings
from core.models import Contact, ProposalBriefing, ProposalQuote
from core.repositories import contacts as contacts_repo
from core.repositories import proposal_briefings as repo
from core.repositories import proposal_quotes as quotes_repo
from core.services import pricing, proposal_quote

_LOGO = Path(__file__).resolve().parent.parent / "web" / "static" / "painel" / "icon-512.png"
_SEP = " · "
_LIST = ", "
_TEXT_MAX = 600

YELLOW = colors.HexColor("#F5C518")
INK = colors.HexColor("#1A1200")
INK_2 = colors.HexColor("#5B5446")
SOFT = colors.HexColor("#FFF6D6")
LINE = colors.HexColor("#E8E1CC")


class NoQuote(Exception):
    """The request has no preliminary quote: there are no values to show."""


@dataclass(frozen=True)
class Data:
    empresa: str
    cuidados: str
    emitida: date
    validade: date
    validade_dias: int
    clinica: bool
    contou: tuple[str, ...]
    escopo: tuple[tuple[str, str], ...]
    implantacao_cents: int
    parcelas: int
    forma: str
    pagamento: str
    mensalidade_cents: int
    fidelidade_cents: int | None
    fidelidade_pct: int
    conversa_cents: Decimal
    usuario_cents: int


def brl(cents: int | Decimal) -> str:
    """"R$ 11.500,00"."""
    value = (Decimal(cents) / 100).quantize(Decimal("0.01"))
    whole, _, fraction = f"{value:,.2f}".partition(".")
    return f"R$ {whole.replace(',', '.')},{fraction}"


def installments(total_cents: int, n: int) -> tuple[int, int]:
    """The first installment and each of the others: equal, the cents left
    over going in the first (as in the model: R$ 1.916,70 and 5 of 1.916,66)."""
    plan = pricing.setup_plan(total_cents, "parcelado", n)
    return plan.first, plan.each


def _number(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _items(value: str) -> list[str]:
    return [item.strip() for item in value.split(_LIST) if item.strip()]


def _and(items: list[str]) -> str:
    """"a, b e c"."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} e {items[-1]}"


def _lower(label: str) -> str:
    """A label inside a sentence: "Atendimento e agenda" → "atendimento e agenda";
    an acronym ("RH") as it is."""
    first = label.split(" ", 1)[0]
    return label if first.isupper() and len(first) > 1 else label[:1].lower() + label[1:]


def _clean(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text[:_TEXT_MAX]


def _who(quem: str, contact: Contact) -> tuple[str, str]:
    """The company and "name, role". A line made of the four fields is read
    by them; anything else leaves the contact's record to name the client."""
    parts = [p.strip() for p in quem.split(_SEP)]
    if len(parts) == 4 and parts[0] and parts[1]:
        nome, empresa, _, cargo = parts
        return empresa, f"{nome}, {cargo}" if cargo and cargo != "-" else nome
    nome = contact.name or ""
    return contact.organization or nome, nome


def assemble(b: ProposalBriefing, contact: Contact, quote: ProposalQuote,
             table: pricing.Table, today: date) -> Data:
    a = b.answers
    clinic = a.get("trilha") == "clinica"
    empresa, cuidados = _who(a.get("quem", ""), contact)

    told: list[str] = []
    lead = _clean(a.get("como_hoje") if clinic else a.get("desafio"))
    if lead:
        told.append(lead)
    if not clinic and _clean(a.get("custo_hoje")):
        told.append(content.render("proposal_pdf.contou_custo", texto=_clean(a.get("custo_hoje"))))
    if clinic and _clean(a.get("equipe")):
        told.append(content.render("proposal_pdf.contou_equipe", texto=_clean(a.get("equipe"))))
    areas = _items(a.get("areas", ""))
    if areas:
        told.append(content.render("proposal_pdf.contou_areas",
                                   lista=_and([_lower(x) for x in areas])))
    tools = [x for x in _items(a.get("sistemas_lista", "")) if x.lower() != "nenhum"]
    if tools:
        told.append(content.render("proposal_pdf.contou_ferramentas", lista=_and(tools)))

    scope: list[tuple[str, str]] = []
    channels = _items(a.get("canais_lista", ""))
    if channels:
        scope.append((content.text("proposal_pdf.escopo_canais"), _and(channels)))
    conversations = int(quote.scope.get("conversas") or 0)
    if conversations:
        scope.append((content.text("proposal_pdf.escopo_volume"),
                      content.render("proposal_pdf.volume", n=_number(conversations))))
    scope.append((content.text("proposal_pdf.escopo_integracoes"),
                  _and(tools) or content.text("proposal_pdf.nenhuma_integracao")))
    users = int(quote.scope.get("usuarios") or 0)
    if users:
        scope.append((content.text("proposal_pdf.escopo_usuarios"),
                      content.render("proposal_pdf.usuarios", n=users)))
    unpriced = {p.removeprefix(proposal_quote.UNPRICED_AREA) for p in quote.premises
                if p.startswith(proposal_quote.UNPRICED_AREA)}
    priced = [x for x in areas if x not in unpriced]
    if priced:
        # Only what the quote priced: an area typed by hand is the team's to add.
        scope.append((content.text("proposal_pdf.escopo_funcoes"), _and(priced)))
    scope.append((content.text("proposal_pdf.escopo_dados"),
                  content.text("proposal_pdf.dados_clinica" if clinic
                               else "proposal_pdf.dados_empresa")))

    discount = table.desconto_fidelidade
    # In cents, exactly the discount the PDF announces.
    loyal = (int((Decimal(quote.monthly_cents) * (1 - discount)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP)) if discount > 0 else None)
    return Data(
        empresa=empresa or content.text("proposal_doc.no_company"), cuidados=cuidados,
        emitida=today, validade=today + timedelta(days=table.validade_dias),
        validade_dias=table.validade_dias, clinica=clinic, contou=tuple(told),
        escopo=tuple(scope), implantacao_cents=quote.setup_cents,
        parcelas=table.parcelas_max, forma=quote.forma_implantacao,
        pagamento=_clean(a.get("pagamento")),
        mensalidade_cents=quote.monthly_cents, fidelidade_cents=loyal,
        fidelidade_pct=int((discount * 100).to_integral_value()),
        conversa_cents=table.por_conversa, usuario_cents=table.por_usuario)


def filename(data: Data) -> str:
    company = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", data.empresa).strip()[:80]
    return content.render("proposal_pdf.arquivo", empresa=company or "cliente")


# ── The layout ───────────────────────────────────────────────────────────────

def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=9.5, leading=13,
                          textColor=INK, alignment=TA_LEFT)
    return {
        "base": base,
        "brand": ParagraphStyle("brand", parent=base, fontName="Helvetica-Bold", fontSize=16),
        "title": ParagraphStyle("title", parent=base, fontName="Helvetica-Bold", fontSize=22,
                                leading=26),
        "subtitle": ParagraphStyle("subtitle", parent=base, fontSize=13, leading=17,
                                   textColor=INK_2),
        "label": ParagraphStyle("label", parent=base, fontSize=8, leading=10, textColor=INK_2),
        "meta": ParagraphStyle("meta", parent=base, fontName="Helvetica-Bold", fontSize=10.5),
        "h3": ParagraphStyle("h3", parent=base, fontName="Helvetica-Bold", fontSize=12,
                             leading=15, spaceBefore=10, spaceAfter=4),
        "small": ParagraphStyle("small", parent=base, fontSize=8.5, leading=12,
                                textColor=INK_2),
        "price": ParagraphStyle("price", parent=base, fontName="Helvetica-Bold", fontSize=15,
                                leading=19),
        "dt": ParagraphStyle("dt", parent=base, fontName="Helvetica-Bold", fontSize=9),
        "told": ParagraphStyle("told", parent=base, spaceAfter=3),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    """Text from the person or the records: shown as written."""
    return Paragraph(escape(text), style)


def _markup(text: str, style: ParagraphStyle) -> Paragraph:
    """Text from content/messages.toml, which may carry <b>."""
    return Paragraph(text, style)


def _cover(data: Data, s: dict[str, ParagraphStyle]) -> list:
    logo = Image(str(_LOGO), width=12 * mm, height=12 * mm)
    brand = Table([[logo, Paragraph("Converge", s["brand"])]],
                  colWidths=[15 * mm, None], hAlign="LEFT")
    brand.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    meta = [
        (content.text("proposal_pdf.label_para"), data.empresa),
        (content.text("proposal_pdf.label_cuidados"), data.cuidados),
        (content.text("proposal_pdf.label_emitida"), f"{data.emitida:%d/%m/%Y}"),
        (content.text("proposal_pdf.label_validade"),
         f"{data.validade:%d/%m/%Y} "
         + content.render("proposal_pdf.validade_dias", dias=data.validade_dias)),
    ]
    cells = [[[_p(label, s["label"]), _p(value, s["meta"])] for label, value in meta[:2]],
             [[_p(label, s["label"]), _p(value, s["meta"])] for label, value in meta[2:]]]
    grid = Table(cells, colWidths=["50%", "50%"])
    grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    band = Table([[[brand, Spacer(1, 6), Paragraph(content.text("proposal_pdf.title"),
                                                     s["title"]),
                    Paragraph(content.text("proposal_pdf.subtitle"), s["subtitle"]),
                    Spacer(1, 8), grid]]], colWidths=["100%"])
    band.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT),
                              ("LINEABOVE", (0, 0), (-1, 0), 4, YELLOW),
                              ("LEFTPADDING", (0, 0), (-1, -1), 14),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                              ("TOPPADDING", (0, 0), (-1, -1), 12),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return [band]


def _section(title: str, s: dict[str, ParagraphStyle]) -> Paragraph:
    return Paragraph(title, s["h3"])


def _prices(data: Data, s: dict[str, ParagraphStyle]) -> Table:
    plan = pricing.setup_plan(data.implantacao_cents, data.forma, data.parcelas)
    if plan.count == 1:
        terms = content.text("proposal_pdf.vista")
    elif plan.forma == "metade":
        terms = content.render("proposal_pdf.metade", primeira=brl(plan.first),
                               segunda=brl(plan.each))
    elif plan.first == plan.each:
        terms = content.render("proposal_pdf.parcelas_iguais", n=plan.count, valor=brl(plan.each))
    else:
        terms = content.render("proposal_pdf.parcelas", n=plan.count, primeira=brl(plan.first),
                               resto=plan.count - 1, valor=brl(plan.each))
    if data.pagamento:
        terms = f"{terms} {content.render('proposal_pdf.pagamento', forma=data.pagamento)}"
    cards = [[_p(content.text("proposal_pdf.implantacao_label"), s["label"]),
              _p(brl(data.implantacao_cents), s["price"]), _p(terms.strip(), s["small"])],
             [_p(content.text("proposal_pdf.mensalidade_label"), s["label"]),
              _p(brl(data.mensalidade_cents), s["price"]),
              _p(content.text("proposal_pdf.mensalidade_nota"), s["small"])]]
    if data.fidelidade_cents is not None:
        cards.append([_p(content.text("proposal_pdf.fidelidade_label"), s["label"]),
                      _p(brl(data.fidelidade_cents), s["price"]),
                      _p(content.render("proposal_pdf.fidelidade_nota",
                                        pct=data.fidelidade_pct), s["small"])])
    row = Table([cards], colWidths=[f"{100 / len(cards):.4f}%"] * len(cards))
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, 0), SOFT),
        ("LINEABOVE", (0, 0), (0, 0), 3, YELLOW),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return row


def _story(data: Data) -> list:
    s = _styles()
    story = _cover(data, s)
    story.append(_section(content.text("proposal_pdf.sec_contou"), s))
    story += [_p(text, s["told"]) for text in data.contou]

    story.append(_section(content.text("proposal_pdf.sec_entrega"), s))
    scope = Table([[_p(term, s["dt"]), _p(value, s["base"])] for term, value in data.escopo],
                  colWidths=[42 * mm, None])
    scope.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINE),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 3),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [scope, Spacer(1, 6), _p(content.text("proposal_pdf.inclui"), s["base"])]

    story.append(KeepTogether([
        _section(content.text("proposal_pdf.sec_investimento"), s), _prices(data, s),
        Spacer(1, 6),
        _p(content.render("proposal_pdf.extras", conversa=brl(data.conversa_cents),
                          usuario=brl(data.usuario_cents)), s["small"])]))

    items: list[Any] = [ListItem(_markup(content.text(f"proposal_pdf.passo_{n}"), s["base"]),
                                 leftIndent=14) for n in range(1, 6)]
    steps = ListFlowable(
        items, bulletType="1", bulletFontName="Helvetica-Bold", bulletColor=INK, leftIndent=14)
    story.append(KeepTogether([_section(content.text("proposal_pdf.sec_passos"), s), steps]))
    return story


def _footer(canvas, doc) -> None:
    canvas.saveState()
    width, _ = A4
    canvas.setStrokeColor(LINE)
    canvas.line(doc.leftMargin, 16 * mm, width - doc.rightMargin, 16 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(INK_2)
    canvas.drawString(doc.leftMargin, 11 * mm, content.text("proposal_pdf.rodape"))
    canvas.drawRightString(width - doc.rightMargin, 11 * mm,
                           content.text("proposal_pdf.rodape_nota"))
    canvas.restoreState()


def build(data: Data) -> bytes:
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=12 * mm, bottomMargin=20 * mm,
                            title=filename(data).removesuffix(".pdf"), author="Converge")
    doc.build(_story(data), onFirstPage=_footer, onLaterPages=_footer)
    return out.getvalue()


def _today() -> date:
    return datetime.now(ZoneInfo(settings.office_timezone)).date()


async def generate(conn, briefing_id: int, *, today: date | None = None) -> tuple[bytes, str]:
    """The PDF of a request and its file name. LookupError when there is no
    such request; NoQuote when it has no quote (or no price table)."""
    b = await repo.get(conn, briefing_id)
    if b is None:
        raise LookupError(briefing_id)
    contact = await contacts_repo.get_by_id(conn, b.contact_id)
    quote = await quotes_repo.get(conn, briefing_id)
    table = pricing.load_table()
    if quote is None or table is None:
        raise NoQuote(briefing_id)
    data = assemble(b, contact or Contact(id=b.contact_id), quote, table, today or _today())
    return build(data), filename(data)
