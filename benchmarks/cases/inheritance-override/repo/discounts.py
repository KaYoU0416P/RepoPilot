from base import Discount


class FixedDiscount(Discount):
    pass


class PercentDiscount(Discount):
    """按百分比打折。value 是百分比，例如 20 表示减 20%。"""
