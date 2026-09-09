from pagination import paginate


def test_full_pages():
    assert paginate([1, 2, 3, 4, 5], 1, 2) == [1, 2]
    assert paginate([1, 2, 3, 4, 5], 2, 2) == [3, 4]


def test_last_partial_page():
    assert paginate([1, 2, 3, 4, 5], 3, 2) == [5]


def test_page_size_one():
    assert paginate([1, 2, 3], 2, 1) == [2]
