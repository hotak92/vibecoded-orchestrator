"""Test file for widgets — exercises the is_test path heuristic axis."""
from __future__ import annotations

from src.widgets import Circle, area_of


def test_area_of_unit_circle() -> None:
    assert round(area_of(1.0), 5) == round(3.141592653589793, 5)


def test_circle_area_matches_helper() -> None:
    circle = Circle(2.0)
    assert circle.area() == area_of(2.0)
