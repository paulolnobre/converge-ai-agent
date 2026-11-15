"""CPF/CNPJ is checked before it reaches Asaas: masks are accepted, a wrong
check digit or a repeated digit is not."""
import pytest

from core.integrations.payments import normalize_tax_id


@pytest.mark.parametrize("raw, expected", [
    ("529.982.247-25", "52998224725"),
    ("52998224725", "52998224725"),
    ("11.222.333/0001-81", "11222333000181"),
    (" 11222333000181 ", "11222333000181"),
])
def test_valid(raw, expected):
    assert normalize_tax_id(raw) == expected


@pytest.mark.parametrize("raw", [
    "529.982.247-24", "111.111.111-11", "00000000000000",
    "11.222.333/0001-82", "1234", "", "abc.def.ghi-jk", "5299822472５",
])
def test_invalid(raw):
    assert normalize_tax_id(raw) is None
