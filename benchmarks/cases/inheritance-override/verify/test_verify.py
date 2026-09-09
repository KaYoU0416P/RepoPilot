from discounts import FixedDiscount, PercentDiscount


def test_percent_discount():
    assert PercentDiscount(20).apply(100) == 80


def test_percent_discount_rounds():
    assert PercentDiscount(15).apply(33.33) == 28.33


def test_fixed_discount_still_works():
    assert FixedDiscount(10).apply(100) == 90


def test_never_goes_negative():
    assert FixedDiscount(500).apply(100) == 0
