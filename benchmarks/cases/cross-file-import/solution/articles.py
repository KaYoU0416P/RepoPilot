from text_utils import slugify


def article_url(title: str) -> str:
    return f"/posts/{slugify(title)}"
