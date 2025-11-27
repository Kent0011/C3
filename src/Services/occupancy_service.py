from collections import deque
import statistics


class OccupancyService:
    def __init__(self, history_len=6, threshold_majority=3):
        """
        history_len: 平滑化に使う過去のフレーム数 (設計では6)
        threshold_majority: 多数決で「有人」とみなす閾値 (設計では3)
        """
        self.history_len = history_len
        self.threshold_majority = threshold_majority

        # 生の推論結果履歴 (0 or 1)
        self.history = deque(maxlen=history_len)

        # 内部状態
        self.current_occupied = False  # 最終的な判定結果

        # ヒステリシス用カウンタ
        # 直近の平滑化結果が連続して変化した回数
        self.consecutive_true_count = 0
        self.consecutive_false_count = 0

    def update_observation(self, obs_count: int) -> bool:
        """
        新しい観測値(obs_count)を受け取り、occupied状態を更新して返す
        """
        # 1. 生データの2値化 (1人以上なら1, 0人なら0)
        raw_val = 1 if obs_count > 0 else 0
        self.history.append(raw_val)

        # データが溜まるまでは判定しない（あるいはFalseとする）
        if len(self.history) < self.history_len:
            return self.current_occupied

        # 2. 多数決による平滑化 (occupied_majority)
        # 履歴の中の 1 の数をカウント
        sum_val = sum(self.history)
        occupied_majority = sum_val >= self.threshold_majority

        # 3. デバウンス付きヒステリシス判定
        # 設計: 直近2サンプル連続で変化した場合のみ状態遷移
        if occupied_majority:
            self.consecutive_true_count += 1
            self.consecutive_false_count = 0
        else:
            self.consecutive_false_count += 1
            self.consecutive_true_count = 0

        # 状態遷移判定
        if not self.current_occupied and self.consecutive_true_count >= 2:
            self.current_occupied = True
        elif self.current_occupied and self.consecutive_false_count >= 2:
            self.current_occupied = False

        return self.current_occupied

    def get_current_status(self):
        return self.current_occupied
