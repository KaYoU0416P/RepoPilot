class User:
    def __init__(self, username: str, nickname: str | None = None):
        self.username = username
        self.nickname = nickname


def display_name(user: User) -> str:
    """有昵称显示昵称（首字母大写），否则显示用户名。"""
    return user.nickname.strip().title()
