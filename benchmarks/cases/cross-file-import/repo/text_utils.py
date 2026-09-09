import re


def slugify(title: str) -> str:
    """标题转 URL slug：小写、非字母数字换成短横线、去掉首尾短横线。"""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
