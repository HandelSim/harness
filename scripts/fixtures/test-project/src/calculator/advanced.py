from __future__ import annotations

import math

from .core import Calculator
from .exceptions import CalculatorError


class ScientificCalculator(Calculator):
    """Calculator that adds a few transcendental ops on top of the basics."""

    def sqrt(self, a: float) -> float:
        if a < 0:
            raise CalculatorError(f"sqrt of negative number {a}")
        result = math.sqrt(a)
        self._record("sqrt", a, 0.0, result)
        return result

    def pow(self, a: float, b: float) -> float:
        result = math.pow(a, b)
        self._record("pow", a, b, result)
        return result

    def log(self, a: float, base: float = math.e) -> float:
        if a <= 0:
            raise CalculatorError(f"log domain error for {a}")
        if base <= 0 or base == 1:
            raise CalculatorError(f"invalid log base {base}")
        result = math.log(a, base)
        self._record("log", a, base, result)
        return result
