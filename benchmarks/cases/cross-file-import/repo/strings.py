"""字符串工具。注意：slugify 已经移到 text_utils.py，这里不再提供。"""


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "..."
