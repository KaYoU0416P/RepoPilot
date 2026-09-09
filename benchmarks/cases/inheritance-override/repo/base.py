class Discount:
    """折扣基类。

    子类**必须**重写 `amount_off(subtotal)`，返回这笔订单要减多少钱。
    基类的实现是「固定减 value 元」。
    """

    def __init__(self, value: float) -> None:
        self.value = value

    def amount_off(self, subtotal: float) -> float:
        return self.value

    def apply(self, subtotal: float) -> float:
        return max(0.0, round(subtotal - self.amount_off(subtotal), 2))
