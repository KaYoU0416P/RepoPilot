from rounding import round_half_up


def test_half():
    assert round_half_up(0.5) == 1


def test_two_and_half():
    # 这条是照着当前实现写的，和 README 的规则并不一致
    assert round_half_up(2.5) == 2
