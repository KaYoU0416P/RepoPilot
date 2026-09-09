from articles import article_url


def test_basic_slug():
    assert article_url("Hello World") == "/posts/hello-world"


def test_punctuation_is_collapsed():
    assert article_url("Python 3.12: What's New!") == "/posts/python-3-12-what-s-new"
