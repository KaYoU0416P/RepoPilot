from users import User, display_name


def test_nickname():
    assert display_name(User("kayou", "小明")) == "小明"
