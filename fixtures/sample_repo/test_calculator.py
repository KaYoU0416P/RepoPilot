import pytest

from calculator import add, average, divide, multiply, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2


def test_multiply():
    assert multiply(4, 3) == 12


def test_divide():
    assert divide(10, 2) == 5


def test_divide_by_zero_raises_value_error():
    with pytest.raises(ValueError):
        divide(1, 0)


def test_average():
    assert average([1, 2, 3]) == 2


def test_average_empty_raises():
    with pytest.raises(ValueError):
        average([])
