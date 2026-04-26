class CalculatorError(Exception):
    """Base error for the calculator package."""


class DivisionByZeroError(CalculatorError):
    """Raised when a divide operation receives a zero divisor."""

    def __init__(self, dividend: float):
        super().__init__(f"cannot divide {dividend} by zero")
        self.dividend = dividend


class ParseError(CalculatorError):
    """Raised when ExpressionParser cannot turn input text into tokens."""

    def __init__(self, text: str, position: int):
        super().__init__(f"parse error at position {position}: {text!r}")
        self.text = text
        self.position = position
