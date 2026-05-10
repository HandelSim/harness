from __future__ import annotations

from .core import Calculator
from .exceptions import ParseError


class ExpressionParser:
    """Tiny infix parser supporting + - * / over numeric literals.

    No precedence handling — left-to-right only. The point of this class is
    to give Serena/Graphify a second non-trivial entry point that imports
    from a sibling module (Calculator), not to ship a real parser.
    """

    OPS = {"+": "add", "-": "sub", "*": "mul", "/": "div"}

    def __init__(self, calc: Calculator | None = None) -> None:
        self.calc = calc or Calculator()

    def tokenize(self, text: str) -> list[str]:
        tokens: list[str] = []
        buf = ""
        for i, ch in enumerate(text):
            if ch.isspace():
                if buf:
                    tokens.append(buf)
                    buf = ""
                continue
            if ch in self.OPS:
                if buf:
                    tokens.append(buf)
                    buf = ""
                tokens.append(ch)
                continue
            if ch.isdigit() or ch == "." or (ch == "-" and not buf and not tokens):
                buf += ch
                continue
            raise ParseError(text, i)
        if buf:
            tokens.append(buf)
        return tokens

    def parse(self, text: str) -> float:
        tokens = self.tokenize(text)
        if not tokens:
            raise ParseError(text, 0)
        acc = float(tokens[0])
        i = 1
        while i < len(tokens) - 1:
            op = tokens[i]
            rhs = float(tokens[i + 1])
            method = getattr(self.calc, self.OPS[op])
            acc = method(acc, rhs)
            i += 2
        return acc
