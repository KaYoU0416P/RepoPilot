from slugify import slugify


def test_basic():
    assert slugify("Deploy Notes") == "deploy-notes"


def test_trailing_punctuation_does_not_leak_a_separator():
    assert slugify("Hello, World!") == "hello-world"
