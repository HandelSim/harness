import pytest

from calculator import Calculator, DivisionByZeroError


def test_add_records_history():
    c = Calculator()
    assert c.add(2, 3) == 5
    assert c.history == [("add", 2, 3, 5)]


def test_sub_mul_div():
    c = Calculator()
    assert c.sub(10, 4) == 6
    assert c.mul(3, 4) == 12
    assert c.div(20, 5) == 4


def test_div_by_zero_raises():
    c = Calculator()
    with pytest.raises(DivisionByZeroError):
        c.div(1, 0)


def test_reset_clears_history():
    c = Calculator()
    c.add(1, 1)
    c.reset()
    assert c.history == []
