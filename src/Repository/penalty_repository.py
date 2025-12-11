from __future__ import annotations

from datetime import datetime
from typing import List, Tuple, Optional

import sqlite3

from Repository.db import get_connection


class BasePenaltyRepository:
    """
    PenaltyService から利用されるリポジトリのインターフェース定義。
    """

    def add_event(self, user_id: str, reason: str, points: int, at: datetime) -> None:
        raise NotImplementedError

    def get_events_since(
        self, user_id: str, since: datetime
    ) -> List[Tuple[datetime, str, int]]:
        """
        指定ユーザについて、since 以降のペナルティイベントを
        (timestamp, reason, points) のリストで返す。
        """
        raise NotImplementedError

    def get_total_penalty_count(self, user_id: str) -> int:
        """
        過去全期間のペナルティ件数（ポイント合計ではなく件数）を返す。
        """
        raise NotImplementedError

    def get_ban_until(self, user_id: str) -> Optional[datetime]:
        raise NotImplementedError

    def set_ban_until(self, user_id: str, ban_until: datetime) -> None:
        raise NotImplementedError

    def clear_ban(self, user_id: str) -> None:
        raise NotImplementedError

    def clear_user(self, user_id: str) -> None:
        """
        デバッグ用: 指定ユーザのペナルティ履歴とBAN状態をすべて削除する。
        """
        raise NotImplementedError


class InMemoryPenaltyRepository(BasePenaltyRepository):
    """
    プロセスメモリ内で完結するペナルティリポジトリ。
    - 開発・テスト用
    """

    def __init__(self) -> None:
        # user_id -> List[{"time": datetime, "reason": str, "points": int}]
        self._events: dict[str, list[dict]] = {}
        # user_id -> ban_until (datetime)
        self._ban_until: dict[str, datetime] = {}

    def add_event(self, user_id: str, reason: str, points: int, at: datetime) -> None:
        self._events.setdefault(user_id, []).append(
            {"time": at, "reason": reason, "points": points}
        )

    def get_events_since(
        self, user_id: str, since: datetime
    ) -> List[Tuple[datetime, str, int]]:
        events = self._events.get(user_id, [])
        return [
            (e["time"], e["reason"], e["points"])
            for e in events
            if e["time"] >= since
        ]

    def get_total_penalty_count(self, user_id: str) -> int:
        return len(self._events.get(user_id, []))

    def get_ban_until(self, user_id: str) -> Optional[datetime]:
        return self._ban_until.get(user_id)

    def set_ban_until(self, user_id: str, ban_until: datetime) -> None:
        self._ban_until[user_id] = ban_until

    def clear_ban(self, user_id: str) -> None:
        self._ban_until.pop(user_id, None)

    def clear_user(self, user_id: str) -> None:
        self._events.pop(user_id, None)
        self._ban_until.pop(user_id, None)


class SqlitePenaltyRepository(BasePenaltyRepository):
    """
    SQLite バックエンドのペナルティリポジトリ実装。
    penalty_events / user_bans テーブルを利用する。
    """

    def add_event(self, user_id: str, reason: str, points: int, at: datetime) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
            INSERT INTO penalty_events (user_id, reason, points, timestamp)
            VALUES (?, ?, ?, ?)
            """,
                (user_id, reason, points, at.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_events_since(
        self, user_id: str, since: datetime
    ) -> List[Tuple[datetime, str, int]]:
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute(
                """
            SELECT timestamp, reason, points
            FROM penalty_events
            WHERE user_id = ? AND timestamp >= ?
            ORDER BY timestamp
            """,
                (user_id, since.isoformat()),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        events: List[Tuple[datetime, str, int]] = []
        for r in rows:
            ts = datetime.fromisoformat(r["timestamp"])
            events.append((ts, r["reason"], int(r["points"])))
        return events

    def get_total_penalty_count(self, user_id: str) -> int:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM penalty_events WHERE user_id = ?", (user_id,)
            )
            row = cur.fetchone()
        finally:
            conn.close()

        return int(row[0]) if row is not None else 0

    def get_ban_until(self, user_id: str) -> Optional[datetime]:
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT ban_until FROM user_bans WHERE user_id = ?", (user_id,)
            )
            row = cur.fetchone()
        finally:
            conn.close()

        if row is None:
            return None
        return datetime.fromisoformat(row["ban_until"])

    def set_ban_until(self, user_id: str, ban_until: datetime) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
            INSERT INTO user_bans (user_id, ban_until)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET ban_until = excluded.ban_until
            """,
                (user_id, ban_until.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def clear_ban(self, user_id: str) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM user_bans WHERE user_id = ?", (user_id,))
            conn.commit()
        finally:
            conn.close()

    def clear_user(self, user_id: str) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM penalty_events WHERE user_id = ?", (user_id,))
            cur.execute("DELETE FROM user_bans WHERE user_id = ?", (user_id,))
            conn.commit()
        finally:
            conn.close()
