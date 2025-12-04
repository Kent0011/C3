from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Optional

from Domain.reservation import Reservation, ReservationStatus
from typing import Any
from Services.penalty_service import PenaltyService


class RoomState(Enum):
    IDLE = auto()  # 対象予約なし
    RESERVED_NOT_USED = auto()  # 対象予約あり・未利用
    IN_USE = auto()  # 利用中
    FINISHED = auto()  # 対象予約のセッション終了（ノーショー含む）


class RoomStateManager:
    def __init__(
        self,
        room_id: str,
        reservation_repo: Any,  # InMemory / Sqlite どちらも受ける
        penalty_service: PenaltyService,
    ):
        self.room_id = room_id
        self.reservation_repo = reservation_repo
        self.penalty_service = penalty_service

        self.current_state: RoomState = RoomState.IDLE
        self.current_reservation_id: Optional[str] = None

        self.grace_period_sec = 10 * 60  # 利用終了後の猶予
        self.arrival_window_before_sec = 10 * 60  # 開始前の「到着してよい」ウィンドウ
        self.arrival_window_after_sec = 15 * 60  # 開始後の「遅刻許容」ウィンドウ
        self.cleanup_margin_sec = 30 * 60  # FINISHED → 次予約に移るまでのマージン

    def update_state(self, is_occupied: bool, current_time: datetime):
        alert: Optional[str] = None

        # 0. 今追いかけている予約を取得
        res: Optional[Reservation] = None
        if self.current_reservation_id is not None:
            res = self.reservation_repo.get_reservation_by_id(
                self.current_reservation_id
            )

        # 1. 予約が消えている / CANCELLED / 完全に過去ならターゲットを取り直す
        cleanup_margin = timedelta(seconds=self.cleanup_margin_sec)
        if (
            res is None
            or res.status == ReservationStatus.CANCELLED
            or current_time > res.end_time + cleanup_margin
        ):
            res = self._select_target_reservation(current_time)
            self.current_reservation_id = res.reservation_id if res else None

            # ここで状態をリセットするのが肝
            # - res is None なら IDLE
            # - 新しい予約を追いかける場合も、必ず IDLE から開始
            self.current_state = RoomState.IDLE

        # ここから先、res が None なら「今は追うべき予約がない」
        if res is None:
            return {
                "state": self.current_state.name,  # IDLE のはず
                "reservation_id": None,
                "is_occupied": is_occupied,
                "alert": None,
            }

        # ===== ステートマシン本体は今のままで OK =====

        # 2. IDLE -> RESERVED_NOT_USED
        if self.current_state == RoomState.IDLE:
            start_window = res.start_time - timedelta(
                seconds=self.arrival_window_before_sec
            )
            if current_time >= start_window:
                self.current_state = RoomState.RESERVED_NOT_USED

        # 3. RESERVED_NOT_USED -> IN_USE / FINISHED (ノーショー)
        if self.current_state == RoomState.RESERVED_NOT_USED:
            valid_arrival_limit = res.start_time + timedelta(
                seconds=self.arrival_window_after_sec
            )

            if is_occupied:
                self.current_state = RoomState.IN_USE
                self.reservation_repo.mark_used(res.reservation_id)
                self._log(
                    f"Check-in detected (res_id={res.reservation_id}, user={res.user_id})"
                )
            elif current_time > valid_arrival_limit:
                if res.status == ReservationStatus.ACTIVE:
                    self.reservation_repo.mark_no_show(res.reservation_id)
                    self.penalty_service.add_penalty(res.user_id, reason="NO_SHOW")
                    self._log(
                        f"No-show detected (res_id={res.reservation_id}, user={res.user_id})"
                    )
                self.current_state = RoomState.FINISHED

        # 4. IN_USE -> FINISHED (終了 or OVERSTAY)
        if self.current_state == RoomState.IN_USE:
            end_limit = res.end_time + timedelta(seconds=self.grace_period_sec)
            if current_time > end_limit:
                if is_occupied:
                    alert = "OVERSTAY"
                    self._log(f"Overstay detected (res_id={res.reservation_id})")
                else:
                    self.current_state = RoomState.FINISHED
                    self._log(f"Session finished (res_id={res.reservation_id})")

        # 5. FINISHED のままにしておくかどうかは「cleanup 条件」で次の呼び出し時に処理される。
        #   ここで IDLE に戻す必要はない（戻すのは 0〜1 でやる）。

        return {
            "state": self.current_state.name,
            "reservation_id": res.reservation_id,
            "is_occupied": is_occupied,
            "alert": alert,
        }

    def _select_target_reservation(self, now: datetime) -> Optional[Reservation]:
        """
        現在時刻 now に対して「追いかけるべき予約」を1件選ぶ。

        - CANCELLED は無視
        - end_time + cleanup_margin を過ぎたものは「過去扱い」で無視
        - 残りから start_time が最も早いものを採用
        """
        candidates = []
        room_res_list = self.reservation_repo.get_reservations_for_room(self.room_id)

        cleanup_margin = timedelta(seconds=self.cleanup_margin_sec)

        for res in room_res_list:
            if res.status == ReservationStatus.CANCELLED:
                continue
            if res.end_time + cleanup_margin < now:
                # 完全に過去の予約
                continue
            candidates.append(res)

        if not candidates:
            return None

        candidates.sort(key=lambda r: r.start_time)
        return candidates[0]

    def _log(self, msg: str) -> None:
        print(f"[RoomStateManager][{self.room_id}] {msg}")
