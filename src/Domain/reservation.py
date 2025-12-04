from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from time_utils import to_jst


class ReservationStatus(str, Enum):
    """
    予約のライフサイクル状態

    - ACTIVE   : 有効な予約（まだ使われていない or 判定前）
    - USED     : 期間内に一度でも入室があり、利用された
    - NO_SHOW  : 期間＋猶予を過ぎても利用されなかった
    - CANCELLED: ユーザ側でキャンセルされた（STEP2ではまだ使わない）
    """

    ACTIVE = "ACTIVE"
    USED = "USED"
    NO_SHOW = "NO_SHOW"
    CANCELLED = "CANCELLED"


@dataclass
class Reservation:
    """
    1件の予約を表現するドメインモデル。時間はすべて JST tz-aware を前提とする。
    """

    reservation_id: str
    room_id: str
    user_id: str
    start_time: datetime
    end_time: datetime
    status: ReservationStatus = field(default=ReservationStatus.ACTIVE)

    def __post_init__(self) -> None:
        """
        - start_time / end_time を JST に正規化
        - end_time > start_time を保証
        """
        self.start_time = to_jst(self.start_time)
        self.end_time = to_jst(self.end_time)

        if self.end_time <= self.start_time:
            raise ValueError(
                f"end_time must be after start_time "
                f"(start={self.start_time}, end={self.end_time})"
            )

    @property
    def is_checked_in(self) -> bool:
        """
        「この予約が一度でも利用されたか」を表す派生プロパティ。
        内部的には status == USED と同義。
        """
        return self.status == ReservationStatus.USED

    def to_dict(self) -> dict[str, Any]:
        """
        APIレスポンスやログ用の簡易シリアライズ。
        ここでは time_utils.format_jst_iso は使わず、あくまでドメインとしての dict を返す。
        """
        return {
            "reservation_id": self.reservation_id,
            "room_id": self.room_id,
            "user_id": self.user_id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "status": self.status.value,
        }
