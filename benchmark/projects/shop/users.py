import hashlib
import secrets
from dataclasses import dataclass


@dataclass
class User:
    email: str
    password_hash: str
    salt: str


class UserStore:
    def __init__(self):
        self._users: dict[str, User] = {}

    def register(self, email: str, password: str) -> User:
        email = email.strip().lower()
        if email in self._users:
            raise ValueError("email already registered")
        salt = secrets.token_hex(8)
        user = User(email, self._hash(password, salt), salt)
        self._users[email] = user
        return user

    def login(self, email: str, password: str) -> bool:
        user = self._users.get(email.strip().lower())
        return bool(user) and user.password_hash == self._hash(password, user.salt)

    @staticmethod
    def _hash(password: str, salt: str) -> str:
        return hashlib.sha256((salt + password).encode()).hexdigest()
