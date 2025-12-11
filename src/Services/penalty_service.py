from __future__ import annotations

from typing import Optional
from datetime import datetime, timedelta
import os

from Repository.penalty_repository import BasePenaltyRepository
from time_utils import now_jst
from dotenv import load_dotenv

load_dotenv()


def _get_int_env(name: str, default: int) -> int:
    """
    環境変数 name を int として読み込む。
    不正値 or 未設定の場合は default を返す。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"[PenaltyConfig] WARN: {name}={raw!r} は int 変換できないため {default} を使用します"
        )
        return default


# ---- ペナルティ設定値（.env から読めるようにする） ----

# 直近何日間のペナルティイベントを見るか
WINDOW_DAYS = _get_int_env("PENALTY_WINDOW_DAYS", 30)

# WINDOW_DAYS 日以内に何回ノーショーしたら BAN にするか
BAN_THRESHOLD = _get_int_env("PENALTY_BAN_THRESHOLD", 3)

# BAN を何日間続けるか
BAN_PERIOD_DAYS = _get_int_env("PENALTY_BAN_PERIOD_DAYS", 7)


class PenaltyService:
    """
    ユーザごとのペナルティを管理するサービス。

    - イベント履歴やBAN状態の永続化は BasePenaltyRepository に委譲
    - 自分は「ウィンドウ」「閾値」「BAN期間」といったビジネスロジックだけを担当
    """

    def __init__(self, repo: BasePenaltyRepository) -> None:
        self._repo = repo

        # 起動時に現在の設定を一度ログに出しておくとデバッグしやすい
        print(
            f"[PenaltyConfig] WINDOW_DAYS={WINDOW_DAYS}, "
            f"BAN_THRESHOLD={BAN_THRESHOLD}, BAN_PERIOD_DAYS={BAN_PERIOD_DAYS}"
        )

    # --- public API ---

    def add_penalty(self, user_id: str, reason: Optional[str] = None) -> int:
        """
        ユーザにペナルティを 1 件付与し、
        付与後の累積ペナルティ件数（全期間）を返す。
        """
        now = now_jst()
        reason_str = reason or "UNKNOWN"

        # 1件イベントを追加（ポイントは現状 1 固定）
        self._repo.add_event(user_id, reason_str, points=1, at=now)

        # 直近 WINDOW_DAYS のポイント数
        points = self.get_points(user_id, now)

        # 全期間累積件数（履歴の件数）
        total = self._repo.get_total_penalty_count(user_id)

        # BAN 判定
        banned_before = self.is_banned(user_id, now)
        if points >= BAN_THRESHOLD and not banned_before:
            ban_until = now + timedelta(days=BAN_PERIOD_DAYS)
            self._repo.set_ban_until(user_id, ban_until)

        # ログ出力
        print(
            f"[Penalty] user={user_id}, total={total}, "
            f"points={points}, banned={self.is_banned(user_id, now)}, reason={reason_str}"
        )

        return total

    def get_penalty(self, user_id: str) -> int:
        """
        指定ユーザの累積ペナルティ件数（全期間）を返す。
        """
        return self._repo.get_total_penalty_count(user_id)

    def get_points(self, user_id: str, now: Optional[datetime] = None) -> int:
        """
        直近 WINDOW_DAYS 日間のポイント合計を返す。
        （いまはイベント1件=1ポイントだが、将来重み付きに拡張可能）
        """
        if now is None:
            now = now_jst()
        since = now - timedelta(days=WINDOW_DAYS)
        events = self._repo.get_events_since(user_id, since)
        # events: List[(timestamp, reason, points)]
        return sum(ev[2] for ev in events)

    def is_banned(self, user_id: str, now: Optional[datetime] = None) -> bool:
        """
        現時点で BAN 中かどうか。
        BAN 期限切れの場合はここで解除する。
        """
        if now is None:
            now = now_jst()

        ban_until = self._repo.get_ban_until(user_id)
        if ban_until is None:
            return False

        # 期限切れならBAN解除
        if now >= ban_until:
            self._repo.clear_ban(user_id)
            return False

        return True

    def get_ban_until(self, user_id: str) -> Optional[datetime]:
        return self._repo.get_ban_until(user_id)

    def get_summary(self, user_id: str, now: Optional[datetime] = None) -> dict:
        """
        API 用に、BAN 状態やポイント等のサマリを返す。
        """
        if now is None:
            now = now_jst()

        points = self.get_points(user_id, now)
        banned = self.is_banned(user_id, now)
        ban_until = self.get_ban_until(user_id)
        total = self._repo.get_total_penalty_count(user_id)

        return {
            "user_id": user_id,
            "points": points,
            "window_days": WINDOW_DAYS,
            "threshold": BAN_THRESHOLD,
            "is_banned": banned,
            "ban_until": ban_until.isoformat() if ban_until else None,
            "total_penalty_count": total,
        }

    def reset_user(self, user_id: str) -> None:
        """
        デバッグ用: 指定ユーザのペナルティ履歴とBAN状態をすべて削除する。
        """
        self._repo.clear_user(user_id)
