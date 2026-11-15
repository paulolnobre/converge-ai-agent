"""Which migrations run: only the new ones, in file-name order."""
from cli import migrate


def test_only_unapplied_files_run_in_name_order():
    available = ["0002_b.sql", "0001_a.sql", "0003_c.sql"]
    assert migrate.pending(available, {"0001_a.sql"}) == ["0002_b.sql", "0003_c.sql"]


def test_nothing_runs_twice():
    assert migrate.pending(["0001_a.sql"], {"0001_a.sql"}) == []
