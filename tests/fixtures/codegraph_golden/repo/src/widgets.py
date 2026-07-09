"""Widget module for the golden-fixture repo.

Exercises: module docstring, imports, classes with inheritance, nested
functions, decorators, type annotations, internal calls (n_callers axis),
and a plain top-level function.
"""
from __future__ import annotations

import math
from typing import List, Optional


def area_of(radius: float) -> float:
    """Return the area of a circle of the given radius."""
    return math.pi * radius * radius


def total_area(radii: List[float]) -> float:
    """Sum the areas of several circles (calls area_of internally)."""
    running = 0.0
    for r in radii:
        running += area_of(r)
    return running


class Shape:
    """Base shape with a name and an abstract-ish area hook."""

    def __init__(self, name: str) -> None:
        self.name = name

    def describe(self) -> str:
        """Return a human label for this shape."""
        return f"shape:{self.name}"


class Circle(Shape):
    """A circle shape that reuses area_of."""

    def __init__(self, radius: float) -> None:
        super().__init__("circle")
        self.radius = radius

    def area(self) -> float:
        """Compute this circle's area via the module helper."""
        return area_of(self.radius)

    def scaled(self, factor: float) -> "Circle":
        """Return a new Circle scaled by factor, using a nested helper."""

        def clamp(value: float) -> float:
            """Nested function: never return a negative scale."""
            return value if value > 0 else 0.0

        return Circle(self.radius * clamp(factor))


def make_optional_label(shape: Optional[Shape]) -> str:
    """Type-annotated function returning a label for an optional shape."""
    if shape is None:
        return "none"
    return shape.describe()
