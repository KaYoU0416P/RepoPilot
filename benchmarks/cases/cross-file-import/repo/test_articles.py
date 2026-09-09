from articles import article_url


def test_url():
    assert article_url("Hello World") == "/posts/hello-world"
