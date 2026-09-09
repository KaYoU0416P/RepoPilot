from discounts import FixedDiscount


def test_fixed():
    assert FixedDiscount(10).apply(100) == 90
