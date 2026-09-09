from money import total_cents


def test_repeated_tenths():
    assert total_cents([0.1] * 10) == 100


def test_classic_float_trap():
    assert total_cents([0.1, 0.2]) == 30


def test_long_sequence():
    assert total_cents([0.07] * 100) == 700


def test_rounds_half_up():
    assert total_cents([1.005]) == 101
