import pytest
from chunking import chunk


def test_even_split():
    assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_non_positive_size_raises():
    with pytest.raises(ValueError):
        chunk([1, 2, 3], 0)
