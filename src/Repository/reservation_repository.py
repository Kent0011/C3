from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import sqlite3

from Domain.reservation import Reservation, ReservationStatus
from Repository.db import get_connection
from time_utils import to_jst, now_jst


class InMemoryReservationRepository:
    """
    予約を部屋ごとに管理する in-memory リポジトリ。

    - 本番では DB バックエンドに差し替える前提で、インターフェースを意識して設計する。
    - SqliteReservationRepository とインターフェースを揃えておくこと。
    """

    def __init__(self, buffer_minutes: int = 5) -> None:
        # room_id -> List[Reservation]
        self._reservations_by_room: Dict[str, List[Reservation]] = {}
        # 予約と予約の間に設ける前後バッファ
        self._buffer: timedelta = timedelta(minutes=buffer_minutes)

    def _get_room_list(self, room_id: str) -> List[Reservation]:
        if room_id not in self._reservations_by_room:
            self._reservations_by_room[room_id] = []
        return self._reservations_by_room[room_id]

    def _generate_reservation_id(self, room_id: str, start: datetime) -> str:
        """
        予約ID生成規則。
        既存実装と互換性を持たせるために、room_id と開始時刻からIDを作る。
        """
        ts = int(start.timestamp())
        return f"{room_id}-{ts}"

    def create_reservation(
        self,
        room_id: str,
        user_id: str,
        start_time: datetime,
        end_time: datetime,
        reservation_id: Optional[str] = None,
    ) -> Reservation:
        """
        予約を1件作成する。

        - start_time / end_time は naive でも tz-aware でもよいが、内部では JST tz-aware に正規化する。
        - 既存予約 + バッファとの重複があれば ValueError を投げる。
        """
        start = to_jst(start_time)
        end = to_jst(end_time)

        if end <= start:
            raise ValueError("end_time must be after start_time")

        room_res_list = self._get_room_list(room_id)
        B = self._buffer

        # バッファ込みの重複チェック
        for res in room_res_list:
            if res.status == ReservationStatus.CANCELLED:
                continue
            s_i = res.start_time
            e_i = res.end_time
            # NG条件: end > s_i - B かつ start < e_i + B
            if end > (s_i - B) and start < (e_i + B):
                raise ValueError(
                    f"Reservation conflicts with existing one: "
                    f"existing={res.reservation_id}, "
                    f"existing_range=({s_i} - {e_i}), "
                    f"new_range=({start} - {end}), "
                    f"buffer={B}"
                )

        if reservation_id is None:
            reservation_id = self._generate_reservation_id(room_id, start)

        new_res = Reservation(
            reservation_id=reservation_id,
            room_id=room_id,
            user_id=user_id,
            start_time=start,
            end_time=end,
            status=ReservationStatus.ACTIVE,
        )
        room_res_list.append(new_res)

        # 開始時刻でソートしておく
        room_res_list.sort(key=lambda r: r.start_time)

        return new_res

    def get_reservations_for_room(self, room_id: str) -> List[Reservation]:
        """
        指定部屋の全予約を開始時刻順に返す。
        """
        room_res_list = self._reservations_by_room.get(room_id, [])
        return list(room_res_list)

    def get_reservation_by_id(self, reservation_id: str) -> Optional[Reservation]:
        """
        ID から予約を1件取得する。見つからなければ None。
        """
        for room_res_list in self._reservations_by_room.values():
            for res in room_res_list:
                if res.reservation_id == reservation_id:
                    return res
        return None

    def get_active_reservation(
        self,
        room_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[Reservation]:
        """
        「今この瞬間」に関係する予約を1件返す（なければ None）。

        - start_time <= now <= end_time を満たす ACTIVE/USED 予約を active とみなす。
        - ノーショー猶予などの扱いは RoomStateManager 側の責務とする。
        """
        if now is None:
            now = now_jst()
        now = to_jst(now)

        room_res_list = self._reservations_by_room.get(room_id, [])
        for res in room_res_list:
            if res.status in (ReservationStatus.ACTIVE, ReservationStatus.USED):
                if res.start_time <= now <= res.end_time:
                    return res
        return None

    def mark_used(self, reservation_id: str) -> bool:
        """
        指定予約を USED 状態にする。成功したら True。
        """
        res = self.get_reservation_by_id(reservation_id)
        if res is None:
            return False
        res.status = ReservationStatus.USED
        return True

    def mark_no_show(self, reservation_id: str) -> bool:
        """
        指定予約を NO_SHOW 状態にする。成功したら True。
        """
        res = self.get_reservation_by_id(reservation_id)
        if res is None:
            return False
        res.status = ReservationStatus.NO_SHOW
        return True

    def cancel_reservation(self, reservation_id: str) -> bool:
        """
        指定予約を CANCELLED 状態にする。成功したら True。
        """
        res = self.get_reservation_by_id(reservation_id)
        if res is None:
            return False
        res.status = ReservationStatus.CANCELLED
        return True


class SqliteReservationRepository:
    """
    SQLite バックエンドの ReservationRepository 実装。

    - InMemoryReservationRepository と同じインターフェースを提供する。
    - reservations テーブルのスキーマは Repository.db.init_db() に依存する。
    """

    def __init__(self, buffer_minutes: int = 5) -> None:
        self._buffer: timedelta = timedelta(minutes=buffer_minutes)

    def _generate_reservation_id(self, room_id: str, start: datetime) -> str:
        ts = int(start.timestamp())
        return f"{room_id}-{ts}"

    def _row_to_reservation(self, row: sqlite3.Row) -> Reservation:
        start = datetime.fromisoformat(row["start_time"])
        end = datetime.fromisoformat(row["end_time"])
        status = ReservationStatus(row["status"])
        return Reservation(
            reservation_id=row["reservation_id"],
            room_id=row["room_id"],
            user_id=row["user_id"],
            start_time=start,
            end_time=end,
            status=status,
        )

    # --- public methods ---

    def create_reservation(
        self,
        room_id: str,
        user_id: str,
        start_time: datetime,
        end_time: datetime,
        reservation_id: Optional[str] = None,
    ) -> Reservation:
        start = to_jst(start_time)
        end = to_jst(end_time)

        if end <= start:
            raise ValueError("end_time must be after start_time")

        conn = get_connection()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1) 予約IDの一意性を先に確保する
        if reservation_id is None:
            base_id = self._generate_reservation_id(room_id, start)
            reservation_id = base_id

            # base_id が既に存在するなら -1, -2,... とサフィックスを付けて空きを探す
            suffix = 1
            while True:
                cur.execute(
                    "SELECT 1 FROM reservations WHERE reservation_id = ?",
                    (reservation_id,),
                )
                row = cur.fetchone()
                if row is None:
                    break  # この reservation_id は未使用なのでOK

                reservation_id = f"{base_id}-{suffix}"
                suffix += 1

        # 2) room_id 単位で既存予約を取得して、バッファ込みの重複チェック
        cur.execute(
            """
            SELECT reservation_id, room_id, user_id,
                   start_time, end_time, status
            FROM reservations
            WHERE room_id = ?
            """,
            (room_id,),
        )
        rows = cur.fetchall()
        B = self._buffer

        for row in rows:
            if row["status"] == ReservationStatus.CANCELLED.value:
                continue
            s_i = datetime.fromisoformat(row["start_time"])
            e_i = datetime.fromisoformat(row["end_time"])
            if end > (s_i - B) and start < (e_i + B):
                conn.close()
                raise ValueError(
                    f"Reservation conflicts with existing one: "
                    f"existing={row['reservation_id']}, "
                    f"existing_range=({s_i} - {e_i}), "
                    f"new_range=({start} - {end}), "
                    f"buffer={B}"
                )

        now = now_jst().isoformat()

        # 3) 挿入時に万一 UNIQUE 制約違反が起きても ValueError に変換して上に返す
        try:
            cur.execute(
                """
                INSERT INTO reservations
                  (reservation_id, room_id, user_id,
                   start_time, end_time, status,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reservation_id,
                    room_id,
                    user_id,
                    start.isoformat(),
                    end.isoformat(),
                    ReservationStatus.ACTIVE.value,
                    now,
                    now,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.close()
            # ここに来るのは、基本的には reservation_id のユニーク制約違反のみのはず
            raise ValueError(f"failed to create reservation: {e}") from e

        conn.close()

        return Reservation(
            reservation_id=reservation_id,
            room_id=room_id,
            user_id=user_id,
            start_time=start,
            end_time=end,
            status=ReservationStatus.ACTIVE,
        )

    def get_reservations_for_room(self, room_id: str) -> List[Reservation]:
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT reservation_id, room_id, user_id,
                   start_time, end_time, status
            FROM reservations
            WHERE room_id = ?
            ORDER BY start_time
            """,
            (room_id,),
        )
        rows = cur.fetchall()
        conn.close()
        return [self._row_to_reservation(r) for r in rows]

    def get_reservation_by_id(self, reservation_id: str) -> Optional[Reservation]:
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT reservation_id, room_id, user_id,
                   start_time, end_time, status
            FROM reservations
            WHERE reservation_id = ?
            """,
            (reservation_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            return None
        return self._row_to_reservation(row)

    def get_active_reservation(
        self,
        room_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[Reservation]:
        if now is None:
            now = now_jst()
        now = to_jst(now)

        # シンプルに全件を取得して Python でフィルタ
        all_res = self.get_reservations_for_room(room_id)
        for res in all_res:
            if res.status in (ReservationStatus.ACTIVE, ReservationStatus.USED):
                if res.start_time <= now <= res.end_time:
                    return res
        return None

    def _update_status(self, reservation_id: str, status: ReservationStatus) -> bool:
        conn = get_connection()
        cur = conn.cursor()
        now = now_jst().isoformat()
        cur.execute(
            """
            UPDATE reservations
            SET status = ?, updated_at = ?
            WHERE reservation_id = ?
            """,
            (status.value, now, reservation_id),
        )
        conn.commit()
        changed = cur.rowcount > 0
        conn.close()
        return changed

    def mark_used(self, reservation_id: str) -> bool:
        return self._update_status(reservation_id, ReservationStatus.USED)

    def mark_no_show(self, reservation_id: str) -> bool:
        return self._update_status(reservation_id, ReservationStatus.NO_SHOW)

    def cancel_reservation(self, reservation_id: str) -> bool:
        return self._update_status(reservation_id, ReservationStatus.CANCELLED)
