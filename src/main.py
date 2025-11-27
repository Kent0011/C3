from flask import Flask, jsonify
import Repository
import logging
import os
import time
import datetime
import threading
from dotenv import load_dotenv

# 新規サービスのインポート
from Services.occupancy_service import OccupancyService
from Services.room_state_manager import RoomStateManager

load_dotenv()
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

app = Flask(__name__)

# --- 設定 ---
POLLING_INTERVAL = 5  # 秒 (設計仕様)
TARGET_CLASS_ID = 0  # 人クラスのID (モデルに合わせて変更してください。一般的に0が多い)

# --- インスタンス初期化 ---
# カメラリポジトリ (Device IDは.envから取得)
ai_camera_repository = Repository.AiCameraRepository(
    console_endpoint=os.getenv("CONSOLE_ENDPOINT"),
    auth_endpoint=os.getenv("AUTH_ENDPOINT"),
    client_id=os.getenv("CLIENT_ID"),
    client_secret=os.getenv("CLIENT_SECRET"),
    device_id=os.getenv("DEVICE_ID"),
)

# 部屋の状態管理 (今回は1部屋のみを想定。複数カメラの場合はリストで管理)
ROOM_ID = "Room-A"
occupancy_service = OccupancyService()
room_manager = RoomStateManager(ROOM_ID)

# 最新の状態を保持する変数（API返却用）
system_status = {}


def background_monitoring_task():
    """
    バックグラウンドでカメラをポーリングし、状態を更新するタスク
    """
    global system_status
    print("Monitoring task started.")

    while True:
        try:
            current_time = datetime.datetime.now()

            # 1. カメラから推論結果取得
            # 注意: リポジトリの実装に合わせて、エラーハンドリング等は適宜追加してください
            result = ai_camera_repository.fetch_inference_result()

            # 2. 人数カウント (obs_count)
            obs_count = 0
            if result:
                # result は {'1': {'C': 0, 'P': 0.9, ...}, '2': ...} のような形式と想定
                for key, val in result.items():
                    if "C" in val and val["C"] == TARGET_CLASS_ID:
                        obs_count += 1

            # 3. 平滑化と占有判定 (Occupancy Service)
            is_occupied = occupancy_service.update_observation(obs_count)

            # 4. 予約・部屋状態の更新 (Room State Manager)
            state_info = room_manager.update_state(is_occupied, current_time)

            # 5. ステータス更新 (API参照用)
            system_status = {
                "timestamp": current_time.isoformat(),
                "room_id": ROOM_ID,
                "obs_count_raw": obs_count,
                "is_occupied_smoothed": is_occupied,
                "room_state": state_info["state"],
                "reservation_id": state_info["reservation_id"],
                "alert": state_info["alert"],
            }

            # ログ出力 (デバッグ用)
            # print(f"Tick: {current_time.strftime('%H:%M:%S')} | Count: {obs_count} | Occ: {is_occupied} | State: {state_info['state']}")

        except Exception as e:
            print(f"Error in monitoring task: {e}")

        time.sleep(POLLING_INTERVAL)


# --- API Routes ---


@app.route("/")
def index():
    """
    現在の部屋の状態と推論の生データを返す
    """
    return jsonify(system_status)


@app.route("/debug/inference")
def debug_inference():
    """
    (既存機能) カメラの推論結果を直接確認
    """
    return ai_camera_repository.fetch_inference_result()


@app.route("/ping")
def ping():
    return "pong"


if __name__ == "__main__":
    # バックグラウンドスレッドの開始
    t = threading.Thread(target=background_monitoring_task, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=8000, debug=False)
    # debug=Trueだとリローダーが走りスレッドが2重起動することがあるので注意
