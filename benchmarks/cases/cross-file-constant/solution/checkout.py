import config


def order_total(subtotal: float) -> float:
    """小计 + 税 + 运费。满额免运费。常量全部来自 config。"""
    tax = subtotal * config.TAX_RATE
    shipping = 0.0 if subtotal >= config.FREE_SHIPPING_THRESHOLD else config.SHIPPING_FEE
    return round(subtotal + tax + shipping, 2)
