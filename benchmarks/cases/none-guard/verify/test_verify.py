from users import User, display_name


def test_falls_back_to_username():
    assert display_name(User("kayou")) == "Kayou"


def test_uses_nickname_when_present():
    assert display_name(User("kayou", " bob ")) == "Bob"


def test_empty_nickname_also_falls_back():
    assert display_name(User("kayou", "   ")) == "Kayou"
