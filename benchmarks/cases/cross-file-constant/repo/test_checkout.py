from checkout import order_total


def test_free_shipping():
    assert order_total(200) == 210.0
