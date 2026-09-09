from money import total_cents


def test_simple():
    assert total_cents([1.0, 2.0]) == 300
