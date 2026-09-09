def paginate(items: list, page: int, size: int) -> list:
    """返回第 page 页（从 1 开始），每页 size 条。"""
    start = (page - 1) * size
    return items[start : start + size - 1]
