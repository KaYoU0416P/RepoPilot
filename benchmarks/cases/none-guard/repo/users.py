class User:
    def __init__(self, username: str, nickname: str | None = None):
        self.username = username
        self.nickname = nickname


def display_name(user: User) -> str:
    """显示名一律首字母大写：昵称去空白后非空就用昵称，否则用用户名。"""
    return user.nickname.strip().title()
