import math

import pytest

from calculator import CalculatorError, ScientificCalculator


def test_inherits_basic_ops():
    c = ScientificCalculator()
    assert c.add(1, 2) == 3
    assert c.mul(3, 4) == 12


def test_sqrt_and_pow():
    c = ScientificCalculator()
    assert c.sqrt(9) == 3
    assert c.pow(2, 10) == 1024


def test_log_default_base_e():
    c = ScientificCalculator()
    assert c.log(math.e) == pytest.approx(1.0)


def test_log_negative_raises():
    c = ScientificCalculator()
    with pytest.raises(CalculatorError):
        c.log(-1)


def test_sqrt_negative_raises():
    c = ScientificCalculator()
    with pytest.raises(CalculatorError):
        c.sqrt(-4)
