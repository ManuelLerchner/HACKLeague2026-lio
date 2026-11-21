from datetime import date
from decimal import Decimal

import pytest

from supplier_chaser.agent.interpret_reply import (
    quantities_in_quote,
    supports_date,
    validate_quantity_unit,
)


@pytest.mark.parametrize("text", ["145 000 szt", "145\u00a0000 szt", "145\u202f000 szt"])
def test_polish_grouped_quantity(text):
    assert quantities_in_quote(text) == {Decimal("145000")}


def test_grouping_does_not_cross_rows():
    assert Decimal("145000") not in quantities_in_quote("145\n000")


@pytest.mark.parametrize("text", ["21.11.2026", "21 listopada 2026"])
def test_polish_date(text):
    assert supports_date(text, date(2026, 11, 21))
    assert not supports_date(text, date(2026, 11, 22))


@pytest.mark.parametrize("unit", ["PLN", "INR", "CNY"])
def test_currency_never_becomes_quantity_unit(unit):
    with pytest.raises(ValueError, match="Currency"):
        validate_quantity_unit(unit, f"2,600 {unit}")


def test_unit_requires_evidence():
    with pytest.raises(ValueError, match="evidence"):
        validate_quantity_unit("PC", "2,600")
    validate_quantity_unit("PC", "2,600 szt")
    validate_quantity_unit(None, "2,600")
