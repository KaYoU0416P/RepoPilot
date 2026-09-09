import config
from checkout import order_total


def test_uses_the_configured_tax_rate():
    assert order_total(200) == round(200 * (1 + config.TAX_RATE), 2)


def test_shipping_below_threshold():
    expected = round(50 * (1 + config.TAX_RATE) + config.SHIPPING_FEE, 2)
    assert order_total(50) == expected


def test_reacts_to_a_changed_tax_rate(monkeypatch):
    """真的读了 config 才会跟着变；写死数字的实现会在这里挂掉。"""
    monkeypatch.setattr(config, "TAX_RATE", 0.20)
    assert order_total(200) == 240.0
