import datetime
from enum import Enum, auto


class RoomState(Enum):
    IDLE = auto()  # 予約なし
    RESERVED_NOT_USED = auto()  # 予約あり・未利用
    IN_USE = auto()  # 利用中
    FINISHED = auto()  # 終了（ノーショー含む）


class Reservation:
    def __init__(self, reservation_id, room_id, start_time, end_time):
        self.reservation_id = reservation_id
        self.room_id = room_id
        self.start_time = start_time
        self.end_time = end_time
        self.is_checked_in = False  # 利用実績フラグ


class RoomStateManager:
    def __init__(self, room_id):
        self.room_id = room_id
        self.current_state = RoomState.IDLE
        self.current_reservation = None

        # 設定値 (分単位を秒などで管理)
        self.grace_period_sec = 10 * 60  # 終了後猶予 10分
        self.arrival_window_before_sec = 10 * 60  # 開始前 10分
        self.arrival_window_after_sec = 15 * 60  # 開始後 15分

    def update_state(self, is_occupied: bool, current_time: datetime.datetime):
        """
        現在の占有状態と時刻をもとに状態遷移を行う
        """

        # 0. 予約情報の取得（モック: 本来は外部APIから取得）
        if self.current_reservation is None:
            self._fetch_next_reservation(current_time)

        # 予約がない場合は IDLE
        if self.current_reservation is None:
            self.current_state = RoomState.IDLE
            return {
                "state": self.current_state.name,
                "reservation": None,
                "alert": None,
            }

        res = self.current_reservation
        alert = None

        # --- ステートマシン ---

        # 1. IDLE -> RESERVED_NOT_USED
        # 開始時間の少し前から予約待機状態にする
        if self.current_state == RoomState.IDLE:
            # 開始時刻 - window <= now
            start_window = res.start_time - datetime.timedelta(
                seconds=self.arrival_window_before_sec
            )
            if current_time >= start_window:
                self.current_state = RoomState.RESERVED_NOT_USED

        # 2. RESERVED_NOT_USED -> IN_USE (利用開始検知)
        if self.current_state == RoomState.RESERVED_NOT_USED:
            # 許容期間内か？
            valid_arrival_limit = res.start_time + datetime.timedelta(
                seconds=self.arrival_window_after_sec
            )

            if is_occupied:
                self.current_state = RoomState.IN_USE
                res.is_checked_in = True
                print(f"[{self.room_id}] Check-in detected.")

            # 3. RESERVED_NOT_USED -> FINISHED (ノーショー判定)
            elif current_time > valid_arrival_limit:
                self.current_state = RoomState.FINISHED
                print(f"[{self.room_id}] No-show detected.")
                # ここで外部にノーショー通知を送る処理が入る

        # 4. IN_USE -> FINISHED (通常終了判定)
        if self.current_state == RoomState.IN_USE:
            end_limit = res.end_time + datetime.timedelta(seconds=self.grace_period_sec)

            # 終了時刻 + grace period を過ぎた
            if current_time > end_limit:
                # 8.3 超過利用の検知
                if is_occupied:
                    # ステートはIN_USEのままだが、アラートを出す設計
                    alert = "OVERSTAY"
                    print(f"[{self.room_id}] Overstay detected!")
                else:
                    # 誰もいなければ終了
                    self.current_state = RoomState.FINISHED
                    print(f"[{self.room_id}] Session finished.")

        # 5. FINISHED -> IDLE (クリーンアップ)
        if self.current_state == RoomState.FINISHED:
            # 次の予約などがなければIDLEへ戻すなどの処理
            # 今回は簡易的に、予約終了時刻を大幅に過ぎたらリセット
            if current_time > res.end_time + datetime.timedelta(minutes=30):
                self.current_reservation = None
                self.current_state = RoomState.IDLE

        return {
            "state": self.current_state.name,
            "reservation_id": res.reservation_id if res else None,
            "is_occupied": is_occupied,
            "alert": alert,
        }

    def _fetch_next_reservation(self, now):
        """
        デモ用のダミー予約生成
        現在時刻の1分後に開始し、5分間続く予約を作成する
        """
        # 既に予約処理済みなら何もしない（簡易実装）
        # 本番ではDB等を参照する
        start = now + datetime.timedelta(minutes=1)
        end = start + datetime.timedelta(minutes=5)

        self.current_reservation = Reservation(
            reservation_id=f"res-{int(now.timestamp())}",
            room_id=self.room_id,
            start_time=start,
            end_time=end,
        )
        print(
            f"[{self.room_id}] New reservation created: {start.strftime('%H:%M')} - {end.strftime('%H:%M')}"
        )
