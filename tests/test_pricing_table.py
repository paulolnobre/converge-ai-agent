"""The price table: optional, read in reais and kept in cents, and refused at
deploy with every broken key named — never a raw exception."""
import shutil
import tomllib
from decimal import Decimal
from pathlib import Path

import pytest

from core import content
from core.config import settings
from core.services import pricing

SHIPPED = Path(__file__).resolve().parent.parent / "content"


@pytest.fixture
def table_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A content directory with the shipped messages and a table the test writes."""
    shutil.copy(SHIPPED / "messages.toml", tmp_path / "messages.toml")
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    pricing.reset()
    content.reset()

    def write(text: str | None):
        target = tmp_path / "pricing.toml"
        if text is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(text, encoding="utf-8")
        pricing.reset()
        content.reset()
    yield write
    pricing.reset()
    content.reset()


def _shipped_with(edit) -> str:
    raw = tomllib.loads((SHIPPED / "pricing.toml").read_text(encoding="utf-8"))
    edit(raw)
    return _dump(raw)


def _dump(value, prefix=()) -> str:
    """A tiny TOML writer for the test's edited copies (tables, numbers, lists, strings)."""
    lines, tables = [], []
    for key, item in value.items():
        if isinstance(item, dict):
            tables.append((key, item))
        else:
            lines.append(f"{key} = {_scalar(item)}")
    out = ([f"[{'.'.join(prefix)}]"] if prefix and lines else []) + lines
    for key, item in tables:
        out.append(_dump(item, prefix + (key,)))
    return "\n".join(out)


def _scalar(item) -> str:
    if isinstance(item, bool):
        return "true" if item else "false"
    if isinstance(item, str):
        return f'"{item}"'
    if isinstance(item, list):
        return "[" + ", ".join(_scalar(i) for i in item) + "]"
    return repr(item)


def test_the_shipped_table_is_valid(monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(SHIPPED))
    pricing.reset()

    assert pricing.problems() == []
    table = pricing.load_table()
    assert table is not None
    assert table.base["basico"] == 200000
    assert table.por_conversa == Decimal("50")
    assert table.infra["vpc_cliente"] == Decimal("1.30")
    assert table.descontos_implantacao == (Decimal("0.05"), Decimal("0.10"))
    assert table.piso_implantacao == 150000 and table.arred_implantacao == 10000
    assert len(table.digest) == 12


def test_without_the_file_pricing_is_off(table_dir):
    table_dir(None)

    assert pricing.load_table() is None
    assert pricing.problems() == []


def test_broken_toml_is_reported_not_raised(table_dir):
    table_dir("[implantacao")

    (problem,) = pricing.problems()
    assert "não é TOML válido" in problem


def test_every_problem_names_its_key(table_dir):
    def edit(raw):
        del raw["implantacao"]["por_canal"]
        raw["implantacao"]["fatores"]["infra"]["compartilhada"] = 0.9
        raw["negociacao"]["parcelas_max"] = 1.5
        raw["implantacao"]["faixa"]["acima"] = 1.2
    table_dir(_shipped_with(edit))

    found = pricing.problems()
    assert len(found) == 4, found
    for key in ("por_canal", "compartilhada", "parcelas_max", "acima"):
        assert any(key in p for p in found), (key, found)


def test_an_alias_in_two_systems_is_refused(table_dir):
    def edit(raw):
        raw["sistemas"]["outlook"].append("Gmail")
    table_dir(_shipped_with(edit))

    assert any("gmail" in p for p in pricing.problems())


def test_aliases_are_normalized(monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(SHIPPED))
    pricing.reset()

    table = pricing.load_table()
    assert table is not None
    assert table.aliases[pricing.normalize("Google Calendar")] == "google_agenda"
    assert pricing.normalize(" Agênda  do GOOGLE ") == "agenda do google"


def test_content_validation_includes_pricing(table_dir):
    def edit(raw):
        del raw["mensalidade"]["sla"]
    table_dir(_shipped_with(edit))

    with pytest.raises(content.ContentError, match="sla"):
        content.validate()


def test_a_missing_rate_is_reported_once(table_dir):
    def edit(raw):
        del raw["mensalidade"]["por_conversa"]
    table_dir(_shipped_with(edit))

    assert pricing.problems() == ["pricing.toml: falta mensalidade.por_conversa."]


@pytest.mark.parametrize("path, value", [
    (("implantacao", "por_canal"), float("nan")),
    (("implantacao", "fatores", "infra", "compartilhada"), float("inf")),
])
def test_non_finite_numbers_are_reported(table_dir, path, value):
    def edit(raw):
        node = raw
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    table_dir(_shipped_with(edit))

    found = pricing.problems()
    assert any(path[-1] in p for p in found), found


def test_a_rounding_step_below_a_cent_is_refused(table_dir):
    def edit(raw):
        raw["implantacao"]["arredondar"] = 0.001
    table_dir(_shipped_with(edit))

    assert any("arredondar" in p for p in pricing.problems())


@pytest.mark.parametrize("systems, key", [("[]", "sistemas"), ('{ gmail = [123] }', "gmail"),
                                          ('{ gmail = [""] }', "gmail")])
def test_systems_must_be_a_table_of_names(table_dir, systems, key):
    text = (SHIPPED / "pricing.toml").read_text(encoding="utf-8")
    head = text.split("[sistemas]")[0]
    table_dir(f"sistemas = {systems}\n" + head)  # root keys go before any [table]

    assert any(key in p for p in pricing.problems())


@pytest.mark.parametrize(("total", "forma", "n", "plan"), [
    (1_150_000, "parcelado", 6, (6, 191_670, 191_666)),  # the model's even split
    (600_000, "parcelado", 6, (6, 100_000, 100_000)),
    (500_000, "parcelado", 1, (1, 500_000, 500_000)),    # one go
    (1_000_000, "metade", 6, (2, 500_000, 500_000)),     # half and half
    (1_000_001, "metade", 6, (2, 500_001, 500_000)),     # the odd cent in the first
    (3, "parcelado", 6, (3, 1, 1)),                       # never more payments than cents
])
def test_the_setup_plan(total, forma, n, plan):
    found = pricing.setup_plan(total, forma, n)
    assert (found.count, found.first, found.each) == plan
    assert found.first + found.each * (found.count - 1) == total
