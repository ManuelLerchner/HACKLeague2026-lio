from datetime import date
from decimal import Decimal

import pytest

from supplier_chaser.agent.interpret_reply import (
    quantities_in_quote,
    source_quote,
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


REPLY = (
    "我方记录显示，数量为460,000件。已确认交货日期为2026年12月2日，贸易条款为FOB盐田。\n\n"
    "该产品不受近期磁铁供应情况影响。\n\n请注意，运输由贵方指定的承运人负责。"
)


def test_reply_quote_may_join_verbatim_sentences_in_order():
    joined = (
        "数量为460,000件。已确认交货日期为2026年12月2日\n\n请注意，运输由贵方指定的承运人负责。"
    )
    assert source_quote(REPLY, joined, segments=True) == joined
    with pytest.raises(ValueError):
        source_quote(REPLY, joined)
    reordered = "请注意，运输由贵方指定的承运人负责。\n\n数量为460,000件。"
    with pytest.raises(ValueError):
        source_quote(REPLY, reordered, segments=True)
    with pytest.raises(ValueError):
        source_quote(REPLY, "数量为460,000件。\n\n数量为999件。", segments=True)


def test_cjk_dates_and_canonical_units():
    assert supports_date("交货日期为2026年12月2日", date(2026, 12, 2))
    assert supports_date("납기 2026년 12월 2일", date(2026, 12, 2))
    assert not supports_date("交货日期为2026年12月20日", date(2026, 12, 2))
    assert validate_quantity_unit("pieces", "460,000 pieces") == "PC"
    assert validate_quantity_unit("件", "460,000件") == "PC"
    assert validate_quantity_unit("KG", "40 KG") == "KG"


def test_stored_unit_words_compare_as_codes():
    from supplier_chaser.domain.units import canonical_unit

    assert canonical_unit("pieces") == canonical_unit("PC") == "PC"
    assert canonical_unit("sztuk") == "PC"
    assert canonical_unit("KG") == "KG"
    assert canonical_unit(None) is None


def test_unit_read_from_word_beside_quoted_quantity():
    from supplier_chaser.agent.interpret_reply import unit_beside_quantity

    assert unit_beside_quantity("We confirm 9,000 pcs on 2027-01-04.", Decimal("9000")) == "PC"
    assert unit_beside_quantity("数量为460,000件。", Decimal("460000")) == "PC"
    assert unit_beside_quantity("We confirm 9,000 on 2027-01-04.", Decimal("9000")) is None
    assert unit_beside_quantity("9,000 boxes of 10 pcs", Decimal("9000")) is None
