"""Tests del calculador de descuento."""

import pytest

from ofertas_hunter.intelligence.discount_calculator import (
    calculate_discount,
    has_minimum_discount,
)


@pytest.mark.parametrize(
    "current,previous,expected",
    [
        (100.0, 200.0, 50.0),
        (50.0, 100.0, 50.0),
        (5.0, 10.0, 50.0),
        (33.33, 100.0, 66.67),
        (200.0, 100.0, 0.0),  # current >= previous
        (100.0, 0.0, None),
        (None, 100.0, None),
        (100.0, None, None),
    ],
)
def test_calculate_discount(current, previous, expected):
    assert calculate_discount(current, previous) == expected


def test_has_minimum_discount_50():
    assert has_minimum_discount(50.0, 100.0, 50) is True
    assert has_minimum_discount(60.0, 100.0, 50) is False
    assert has_minimum_discount(None, 100.0, 50) is False
