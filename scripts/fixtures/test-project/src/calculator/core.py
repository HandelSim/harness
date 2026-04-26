from __future__ import annotations

from .exceptions import DivisionByZeroError


class Calculator:
    """Plain four-function calculator with a small running history."""

    def __init__(self) -> None:
        self.history: list[tuple[str, float, float, float]] = []

    def add(self, a: float, b: float) -> float:
        result = a + b
        self._record("add", a, b, result)
        return result

    def sub(self, a: float, b: float) -> float:
        result = a - b
        self._record("sub", a, b, result)
        return result

    def mul(self, a: float, b: float) -> float:
        result = a * b
        self._record("mul", a, b, result)
        return result

    def div(self, a: float, b: float) -> float:
        if b == 0:
            raise DivisionByZeroError(a)
        result = a / b
        self._record("div", a, b, result)
        return result

    def reset(self) -> None:
        self.history.clear()

    def _record(self, op: str, a: float, b: float, result: float) -> None:
        self.history.append((op, a, b, result))
