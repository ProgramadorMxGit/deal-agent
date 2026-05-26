"""Tests del parser de precios genérico."""

from __future__ import annotations

import pytest

from ofertas_hunter.extraction.price_parser import (
    calculate_discount,
    detect_monthly_payment,
    extract_discount_percent,
    parse_price_text,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$1,299.00", 1299.0),
        ("$1.299,00", 1299.0),
        ("$388", 388.0),
        ("MXN 1,299.50", 1299.5),
        ("1,299", 1299.0),
        ("1,29", 1.29),
        ("", None),
        (None, None),
        ("texto sin números", None),
    ],
)
def test_parse_price_text(text, expected):
    assert parse_price_text(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("-65%", 65),
        ("65% de descuento", 65),
        ("Ahorra 50%", 50),
        ("100%", None),
        ("", None),
        ("0%", None),
        ("texto sin números", None),
    ],
)
def test_extract_discount_percent(text, expected):
    assert extract_discount_percent(text) == expected


def test_calculate_discount_basic():
    assert calculate_discount(388.0, 899.0) == 56.84
    assert calculate_discount(50.0, 100.0) == 50.0
    assert calculate_discount(100.0, 100.0) == 0.0
    assert calculate_discount(120.0, 100.0) == 0.0


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Desde $899.00 / mes", True),
        ("12 pagos de $899.00 al mes", True),
        ("12 meses sin intereses", True),
        ("MSI 18 meses", True),
        ("$1,299.00 por mes", True),
        ("$1,299.00 mensuales", True),
        ("$1,299.00", False),
        ("Precio total: $1,299.00", False),
        ("", False),
    ],
)
def test_detect_monthly_payment(text, expected):
    assert detect_monthly_payment(text) is expected
