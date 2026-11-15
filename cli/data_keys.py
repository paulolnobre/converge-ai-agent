"""Check the field-encryption keys without showing them.

    python -m cli.data_keys check

Says how many DATA_KEYS there are and which version encrypts, and whether
DATA_INDEX_KEY is well formed. The keys are generated on the server by
deploy/data-keys.sh (the container cannot write the server's .env).
"""
from __future__ import annotations

import sys

from core.config import parse_data_keys, parse_index_key, settings


def check() -> int:
    try:
        keys = parse_data_keys(settings.data_keys) if settings.data_keys else {}
        if not keys:
            print("DATA_KEYS: vazia")
            return 1
        print(f"DATA_KEYS: {len(keys)} chave(s), escreve com a versão {next(iter(keys))}")
        if not settings.data_index_key:
            print("DATA_INDEX_KEY: vazia")
            return 1
        parse_index_key(settings.data_index_key)
        print("DATA_INDEX_KEY: ok")
    except ValueError as exc:
        print(f"Erro: {exc}")
        return 1
    return 0


def main(argv: list[str]) -> int:
    if argv[1:] != ["check"]:
        print(__doc__)
        return 2
    return check()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
