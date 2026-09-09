class Registry:
    def __init__(self) -> None:
        self._emails: set[str] = set()

    def register(self, email: str) -> None:
        self._emails.add(email)

    def is_registered(self, email: str) -> bool:
        """邮箱不区分大小写。"""
        return email in self._emails
