import re


def slugify(title: str) -> str:
    """标题 → URL slug。"""
    lowered = title.lower().strip()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered)
    return cleaned
