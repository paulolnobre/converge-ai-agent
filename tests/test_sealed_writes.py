"""No SQL in the app writes an encrypted field in clear.

Every INSERT or UPDATE that names a field of the registry must name its
``_sealed`` column too: that pair is what `sealed_fields.write` produces (the
old column emptied, the sealed one filled). An old column written alone is a
customer's text going into the database in clear — and, once cli.seal_data has
locked the old columns, an error in production.

Static, so it cannot see column lists built at runtime (`update_details`,
`funnel.update_lead`); those are covered by the Postgres tests.
"""
import re
from pathlib import Path

from core.sealed_fields import FIELDS

ROOT = Path(__file__).resolve().parent.parent
EXEMPT = {"cli/seal_data.py"}  # the conversion itself writes both columns by name
TABLES = {f.table for f in FIELDS}


def _flatten(source: str) -> str:
    """Join adjacent string literals, so SQL split over lines reads as one."""
    return re.sub(r"""["']\s*\n\s*[rf]?["']""", "", source)


def _targets(sql_tail: str, table: str, kind: str) -> set[str]:
    if kind == "INSERT":
        match = re.match(r"\s*\(([^)]*)\)", sql_tail)
        return {c.strip() for c in match.group(1).split(",")} if match else set()
    match = re.match(r"\s+SET\s+(.*?)(?:\bWHERE\b|\bRETURNING\b|\bFROM\b|$)", sql_tail, re.S)
    if not match:
        return set()
    return set(re.findall(r"\b(\w+)\s*=", match.group(1)))


def _violations() -> list[str]:
    found = []
    for path in sorted([*ROOT.glob("core/**/*.py"), *ROOT.glob("cli/*.py")]):
        relative = path.relative_to(ROOT).as_posix()
        if relative in EXEMPT:
            continue
        text = _flatten(path.read_text())
        for match in re.finditer(r"\b(INSERT INTO|UPDATE)\s+(\w+)", text):
            kind, table = match.group(1).split()[0], match.group(2)
            if table not in TABLES:
                continue
            targets = _targets(text[match.end():match.end() + 800], table, kind)
            for f in FIELDS:
                if f.table == table and f.column in targets and f.sealed not in targets:
                    line = text[:match.start()].count("\n") + 1
                    found.append(f"{relative}:~{line} writes {f.context} in clear")
    return found


def test_no_sql_writes_a_sealed_column_in_clear():
    violations = _violations()
    assert violations == [], "\n".join(violations)


def test_the_check_catches_a_clear_write(tmp_path, monkeypatch):
    """The scanner itself: a plain INSERT of a name is a violation, the pair
    is not."""
    assert _targets(" (chat_id, name, source) VALUES", "contacts", "INSERT") == {
        "chat_id", "name", "source"}
    assert _targets(" SET name = %s, name_sealed = %s WHERE id = %s", "contacts",
                    "UPDATE") == {"name", "name_sealed"}
