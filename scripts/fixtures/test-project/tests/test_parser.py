import pytest

from calculator import ExpressionParser, ParseError


def test_tokenize_simple():
    p = ExpressionParser()
    assert p.tokenize("1 + 2") == ["1", "+", "2"]


def test_tokenize_no_spaces():
    p = ExpressionParser()
    assert p.tokenize("3*4") == ["3", "*", "4"]


def test_parse_left_to_right():
    p = ExpressionParser()
    # No precedence: ((1 + 2) * 3) = 9
    assert p.parse("1 + 2 * 3") == 9


def test_parse_invalid_character_raises():
    p = ExpressionParser()
    with pytest.raises(ParseError):
        p.tokenize("2 ^ 3")


def test_parse_division():
    p = ExpressionParser()
    assert p.parse("10 / 2") == 5
