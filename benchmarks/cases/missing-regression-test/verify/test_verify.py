from pathlib import Path

import pytest
from chunking import chunk


def test_zero_size_raises_instead_of_hanging():
    with pytest.raises(ValueError):
        chunk([1, 2, 3], 0)


def test_negative_size_raises():
    with pytest.raises(ValueError):
        chunk([1, 2, 3], -1)


def test_normal_behaviour_is_unchanged():
    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_a_regression_test_was_actually_added():
    """任务要求补测试，就要真的补。只改实现不算完成。"""
    source = Path("test_chunking.py").read_text(encoding="utf-8")
    assert "raises" in source and "chunk(" in source
