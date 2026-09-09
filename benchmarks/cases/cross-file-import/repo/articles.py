from strings import slugify


def article_url(title: str) -> str:
    return f"/posts/{slugify(title)}"
