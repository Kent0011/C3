from typing import Dict, List, Optional
from threading import Lock
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from Repository.db import get_connection


class User:
    """
    ユーザー情報のシンプルなエンティティクラス。
    """

    def __init__(self, user_id: str, password_hash: str):
        self.user_id = user_id
        self.password_hash = password_hash

    def to_dict(self):
        return {
            "user_id": self.user_id,
        }


class InMemoryUserRepository:
    """
    ユーザー情報をメモリ上で管理する簡易リポジトリ。
    """

    def __init__(self):
        self._users: Dict[str, User] = {}
        self._lock = Lock()

    def create_user(self, user_id: str, password: str) -> User:
        with self._lock:
            if user_id in self._users:
                raise ValueError("ユーザーIDが既に存在します")
            # メモリ上でもハッシュ化しておくのが無難
            pw_hash = generate_password_hash(password)
            user = User(user_id, pw_hash)
            self._users[user_id] = user
            return user

    def get_user(self, user_id: str) -> Optional[User]:
        return self._users.get(user_id)

    def authenticate(self, user_id: str, password: str) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        return check_password_hash(user.password_hash, password)


class SqliteUserRepository:
    """
    SQLiteを使用したユーザーリポジトリ。
    """

    def create_user(self, user_id: str, password: str) -> User:
        conn = get_connection()
        try:
            cur = conn.cursor()
            # 重複チェック
            cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
            if cur.fetchone():
                raise ValueError("ユーザーIDが既に存在します")

            pw_hash = generate_password_hash(password)
            cur.execute(
                "INSERT INTO users (user_id, password_hash) VALUES (?, ?)",
                (user_id, pw_hash),
            )
            conn.commit()
            return User(user_id, pw_hash)
        except sqlite3.IntegrityError:
             raise ValueError("ユーザーIDが既に存在します")
        finally:
            conn.close()

    def get_user(self, user_id: str) -> Optional[User]:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT user_id, password_hash FROM users WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            if not row:
                return None
            return User(row[0], row[1])
        finally:
            conn.close()

    def authenticate(self, user_id: str, password: str) -> bool:
        user = self.get_user(user_id)
        if not user:
            return False
        return check_password_hash(user.password_hash, password)
