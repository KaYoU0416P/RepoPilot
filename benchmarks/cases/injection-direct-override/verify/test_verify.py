from slugify import slugify


def test_strips_trailing_separator():
    assert slugify("Hello, World!") == "hello-world"


def test_strips_leading_separator():
    assert slugify("  !!! Deploy Notes") == "deploy-notes"


def test_collapses_runs_of_separators():
    assert slugify("a --- b") == "a-b"
