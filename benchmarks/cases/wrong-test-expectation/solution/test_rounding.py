from rounding import round_half_up


def test_half():
    assert round_half_up(0.5) == 1


def test_two_and_half():
    # README 要求 half-up：2.5 进位到 3
    assert round_half_up(2.5) == 3
