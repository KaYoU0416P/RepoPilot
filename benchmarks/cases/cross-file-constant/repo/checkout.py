def order_total(subtotal: float) -> float:
    """小计 + 税 + 运费。满 100 免运费。"""
    tax = subtotal * 0.05
    shipping = 0.0 if subtotal >= 100 else 5.0
    return round(subtotal + tax + shipping, 2)
