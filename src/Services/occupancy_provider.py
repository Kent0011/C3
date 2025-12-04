from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Protocol
from Repository.ai_camera_repository import AiCameraRepository


class OccupancyProvider(ABC):
    """
    部屋の占有状態を提供する抽象インターフェース。
    """

    @abstractmethod
    def get_is_occupied(self, current_time: datetime) -> bool:
        """
        現在時刻における「部屋が使われているかどうか」を返す。
        """
        raise NotImplementedError


class CameraOccupancyProvider(OccupancyProvider):
    """
    実機AIカメラの推論結果から占有状態を判定するプロバイダ。
    """

    def __init__(self, ai_repo: AiCameraRepository):
        self._repo = ai_repo

    def get_is_occupied(self, current_time: datetime) -> bool:
        """
        - ai_repo.fetch_inference_result() 等を呼び、
        - 推論結果から「人が1人以上いるか」を判定して True/False を返す。
        """
        # ここは実装時に詰めるが、イメージとしては：
        result = self._repo.fetch_inference_result()
        if not result:
            return False

        # 例えば "person" クラスの検出数を数える等
        # len(result) や特定キーを見る形でもよい
        detected_count = self._count_person_objects(result)
        return detected_count > 0

    def _count_person_objects(self, result: dict) -> int:
        """
        推論結果から「人」とみなすオブジェクト数を数えるヘルパー。
        実際にはクラスIDやラベルに応じて調整する。
        """
        # 今は「とりあえず全部のオブジェクト数を数える」でスタブしておいても良い
        # 後で class_id を見て精緻化する。
        return sum(1 for k in result.keys() if k.isdigit())


class DummyOccupancyProvider(OccupancyProvider):
    """
    デバッグ用: 手動で占有状態を切り替えるプロバイダ。
    /debug/occupancy API からフラグを書き換える想定。
    """

    def __init__(self, initial: bool = False):
        self._occupied: bool = initial

    def set_occupied(self, value: bool) -> None:
        self._occupied = bool(value)

    def get_is_occupied(self, current_time: datetime) -> bool:
        return self._occupied
