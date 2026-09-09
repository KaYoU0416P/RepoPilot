class Registry:
    def __init__(self) -> None:
        self._emails: set[str] = set()

    @staticmethod
    def _normalise(email: str) -> str:
        return email.strip().lower()

    def register(self, email: str) -> None:
        self._emails.add(self._normalise(email))

    def is_registered(self, email: str) -> bool:
        """邮箱不区分大小写。"""
        return self._normalise(email) in self._emails
