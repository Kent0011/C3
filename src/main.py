from flask import Flask, jsonify, request, render_template, redirect, url_for, session
import Repository
import logging
import os
import time
from datetime import datetime, timedelta
import threading
from dotenv import load_dotenv
from time_utils import (
    now_jst,
    format_jst_iso,
    parse_jst_datetime,
    set_simulated_time,
    clear_simulated_time,
    get_time_status,
)
from Repository.reservation_repository import (
    InMemoryReservationRepository,
    SqliteReservationRepository,
)
from Repository.penalty_repository import (
    InMemoryPenaltyRepository,
    SqlitePenaltyRepository,
)
from Repository.user_repository import (
    InMemoryUserRepository,
    SqliteUserRepository,
)
from Repository.db import init_db
from Services.penalty_service import PenaltyService
from Services.room_state_manager import RoomStateManager
from Services.occupancy_provider import (
    OccupancyProvider,
    CameraOccupancyProvider,
    DummyOccupancyProvider,
)

load_dotenv()

# 予約時間に関する設定（分や日数の制約）
MIN_RESERVE_MINUTES = int(os.getenv("MIN_RESERVE_MINUTES", "15"))  # 最短 15 分
MAX_RESERVE_MINUTES = int(os.getenv("MAX_RESERVE_MINUTES", "120"))  # 最長 120 分
MAX_RESERVE_DAYS_AHEAD = int(os.getenv("MAX_RESERVE_DAYS_AHEAD", "7"))  # 7日先まで

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "dev_secret_key")  # セッション利用に必須

# --- 設定 ---
POLLING_INTERVAL = 5  # 秒 (設計仕様)

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

# 環境変数 USE_SQLITE でバックエンドを切り替え
USE_SQLITE = os.getenv("USE_SQLITE", "false").lower() == "true"

if USE_SQLITE:
    print("[Config] Using SqliteReservationRepository / SqlitePenaltyRepository / SqliteUserRepository")
    # SQLite テーブルがなければ作成
    init_db()
    reservation_repo = SqliteReservationRepository(buffer_minutes=5)
    penalty_repo = SqlitePenaltyRepository()
    user_repo = SqliteUserRepository()
else:
    print("[Config] Using InMemoryReservationRepository / InMemoryPenaltyRepository / InMemoryUserRepository")
    reservation_repo = InMemoryReservationRepository(buffer_minutes=5)
    penalty_repo = InMemoryPenaltyRepository()
    user_repo = InMemoryUserRepository()

penalty_service = PenaltyService(penalty_repo)

room_manager = RoomStateManager(
    room_id=ROOM_ID,
    reservation_repo=reservation_repo,
    penalty_service=penalty_service,
)

# 最新の状態を保持する変数（API返却用）
system_status = {
    "timestamp": None,  # まだ監視タスクが動いていないので None
    "room_id": ROOM_ID,  # ここだけは決め打ちでよい
    "room_state": "IDLE",  # RoomState.IDLE 相当（予約追跡なし）
    "people_count": 0,  # 未計測なので 0
    "is_used": False,  # 未使用
    "reservation_id": None,  # 追跡中の予約なし
    "alert": None,  # アラートなし
}

occupancy_provider: OccupancyProvider | None = None


def background_monitoring_task():
    global system_status, occupancy_provider
    print("Monitoring task started.")

    while True:
        try:
            current_time = now_jst()

            is_occupied = occupancy_provider.get_is_occupied(current_time)

            state_info = room_manager.update_state(is_occupied, current_time)

            # Stub: 占有中なら 1, 空なら 0 として人数を入れておく
            people_count = 1 if is_occupied else 0

            system_status = {
                "timestamp": format_jst_iso(current_time),
                "room_id": ROOM_ID,
                "people_count": people_count,
                "is_used": is_occupied,
                "room_state": state_info["state"],
                "reservation_id": state_info["reservation_id"],
                "alert": state_info["alert"],
            }

        except Exception as e:
            print(f"Error in monitoring task: {e}")

        time.sleep(POLLING_INTERVAL)


def create_occupancy_provider(
    ai_repo: Repository.AiCameraRepository,
) -> OccupancyProvider:
    """
    環境変数 OCCUPANCY_MODE に応じて、
    CameraOccupancyProvider / DummyOccupancyProvider のどちらかを返す。
    """
    mode = os.getenv("OCCUPANCY_MODE", "dummy").lower()

    if mode == "camera":
        print("[Config] Using CameraOccupancyProvider")
        return CameraOccupancyProvider(ai_repo)
    elif mode == "dummy":
        print("[Config] Using DummyOccupancyProvider")
        return DummyOccupancyProvider(initial=False)
    else:
        # 想定外の値なら dummy にフォールバック
        print(f"[Config] Unknown OCCUPANCY_MODE={mode}, fallback to dummy")
        return DummyOccupancyProvider(initial=False)


# --- API Routes ---


@app.route("/api/status")
def get_status():
    """
    現在の部屋の状態と推論の生データを返す
    """
    return jsonify(system_status)


@app.route("/")
def index():
    return render_template("landing.html")


@app.route("/debug/ui")
def debug_ui():
    """
    デバッグ用Web UI。
    - 予約作成
    - 占有状態の切り替え（dummyモード）
    - 現在ステータス表示
    - 予約一覧表示
    - ペナルティ表示
    をブラウザから行えるようにする。
    """
    return render_template("debug.html")


@app.route("/debug/inference")
def debug_inference():
    """
    (既存機能) カメラの推論結果を直接確認
    """
    return ai_camera_repository.fetch_inference_result()


@app.route("/debug/reservations", methods=["POST"])
def debug_create_reservation():
    """
    デバッグ用: 予約を1件作成する。
    body 例:
    {
        "user_id": "user-001",
        "room_id": "Room-A",                    # 省略時は ROOM_ID
        "start_time": "2025-11-29T14:50:00+09:00",
        "end_time":   "2025-11-29T15:20:00+09:00"
    }
    """
    data = request.get_json(force=True) or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    room_id = data.get("room_id", ROOM_ID)
    start_str = data.get("start_time")
    end_str = data.get("end_time")
    if not start_str or not end_str:
        return jsonify({"error": "start_time and end_time are required"}), 400

    try:
        start_dt = parse_jst_datetime(start_str)
        end_dt = parse_jst_datetime(end_str)
        res = reservation_repo.create_reservation(
            room_id=room_id,
            user_id=user_id,
            start_time=start_dt,
            end_time=end_dt,
        )
    except ValueError as e:
        # 重複やフォーマットエラーなど
        return jsonify({"error": str(e)}), 400

    return jsonify(res.to_dict()), 201


@app.route("/debug/reservations", methods=["GET"])
def debug_list_reservations():
    """
    デバッグ用: 現在の予約一覧を返す。
    クエリ param room_id を指定しなければ ROOM_ID。
    """
    room_id = request.args.get("room_id", ROOM_ID)
    res_list = reservation_repo.get_reservations_for_room(room_id)
    return jsonify([r.to_dict() for r in res_list])


@app.route("/debug/occupancy", methods=["POST"])
def debug_set_occupancy():
    """
    デバッグ用: ダミーモード時に占有状態をオン/オフ切り替えるAPI。

    body 例:
    { "occupied": true }
    """
    global occupancy_provider

    if not isinstance(occupancy_provider, DummyOccupancyProvider):
        return jsonify({"error": "DummyOccupancyProvider is not active"}), 400

    data = request.get_json(force=True) or {}
    occupied = bool(data.get("occupied"))

    occupancy_provider.set_occupied(occupied)

    return jsonify({"occupied": occupied})


@app.route("/debug/state_params", methods=["GET", "POST"])
def debug_state_params():
    """
    RoomStateManager の時間パラメータを確認・変更するためのデバッグ用エンドポイント。
    GET: 現在値を返す
    POST: 指定された値だけ上書きする
    """
    from flask import jsonify, request

    # ここで room_manager はグローバルを前提にしている
    if request.method == "GET":
        return jsonify(
            {
                "grace_period_sec": room_manager.grace_period_sec,
                "arrival_window_before_sec": room_manager.arrival_window_before_sec,
                "arrival_window_after_sec": room_manager.arrival_window_after_sec,
                "cleanup_margin_sec": room_manager.cleanup_margin_sec,
            }
        )

    # POST の場合
    data = request.get_json(force=True) or {}

    def _int_or_default(key, current):
        try:
            return int(data[key])
        except (KeyError, TypeError, ValueError):
            return current

    room_manager.grace_period_sec = _int_or_default(
        "grace_period_sec", room_manager.grace_period_sec
    )
    room_manager.arrival_window_before_sec = _int_or_default(
        "arrival_window_before_sec", room_manager.arrival_window_before_sec
    )
    room_manager.arrival_window_after_sec = _int_or_default(
        "arrival_window_after_sec", room_manager.arrival_window_after_sec
    )
    room_manager.cleanup_margin_sec = _int_or_default(
        "cleanup_margin_sec", room_manager.cleanup_margin_sec
    )

    return jsonify(
        {
            "grace_period_sec": room_manager.grace_period_sec,
            "arrival_window_before_sec": room_manager.arrival_window_before_sec,
            "arrival_window_after_sec": room_manager.arrival_window_after_sec,
            "cleanup_margin_sec": room_manager.cleanup_margin_sec,
        }
    )


@app.route("/debug/time", methods=["GET"])
def debug_get_time():
    """
    デバッグ用: 仮想時計の設定状況を返す。
    """
    return jsonify(get_time_status())


@app.route("/debug/time", methods=["POST"])
def debug_set_time():
    data = request.get_json(force=True) or {}
    mode = data.get("mode")

    if mode == "real":
        clear_simulated_time()
        return jsonify(get_time_status())

    if mode == "simulated":
        scale = float(data.get("scale", 1.0))
        # "now" が指定されていればそれを仮想時刻に、なければ実時間を使う
        if "now" in data:
            try:
                sim_now = parse_jst_datetime(data["now"])
            except Exception as e:
                return jsonify({"error": f"invalid now: {e}"}), 400
        else:
            sim_now = now_jst()  # 実時計ベース

        try:
            set_simulated_time(sim_now, scale=scale)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify(get_time_status())

    return jsonify({"error": "mode must be 'real' or 'simulated'"}), 400


@app.route("/debug/penalties/<user_id>", methods=["GET"])
def debug_get_penalty(user_id: str):
    """
    デバッグ用: 指定ユーザの累積ペナルティ数を返す。
    """
    total = penalty_service.get_penalty(user_id)
    return jsonify({"user_id": user_id, "penalty_count": total})


@app.route("/debug/penalties/reset/<user_id>", methods=["POST"])
def debug_reset_penalty(user_id: str):
    penalty_service.reset_user(user_id)
    return jsonify({"user_id": user_id, "reset": True})


@app.route("/app/ui")
def app_ui():
    """
    一般ユーザー向けの簡易予約画面。
    セッション認証を使用。
    """
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    return render_template("app.html", user_id=user_id)


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    ログイン画面
    """
    if request.method == "POST":
        user_id = request.form.get("user_id")
        password = request.form.get("password")

        if not user_id or not password:
            return render_template("login.html", error="IDとパスワードは必須です")

        if user_repo.authenticate(user_id, password):
            # 認証成功したらセッションに保存してリダイレクト
            session["user_id"] = user_id
            return redirect(url_for("app_ui"))
        else:
            return render_template("login.html", error="IDまたはパスワードが間違っています")

    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """
    アカウント作成画面
    """
    if request.method == "POST":
        user_id = request.form.get("user_id")
        password = request.form.get("password")

        if not user_id or not password:
            return render_template("signup.html", error="IDとパスワードは必須です")

        try:
            user_repo.create_user(user_id, password)
            # 作成成功したらログイン状態にしてリダイレクト
            session["user_id"] = user_id
            return redirect(url_for("app_ui"))
        except ValueError as e:
            return render_template("signup.html", error=str(e))

    return render_template("signup.html")


@app.route("/api/room_status")
def api_room_status():
    """
    現在の部屋状態（room_state, is_occupied, reservation_id, alert 等）を返す。
    ※現状の index() と同じ中身で構わない。
    """
    return jsonify(system_status)


@app.route("/api/reservations", methods=["GET"])
def api_list_reservations():
    user_id = request.args.get("user_id")
    date_str = request.args.get("date")  # "2025-11-29" など

    all_res = reservation_repo.get_reservations_for_room(ROOM_ID)

    result = []
    for r in all_res:
        # user_id で絞る
        if user_id and r.user_id != user_id:
            continue

        # date で絞る（start_time の日付で判定）
        if date_str:
            try:
                target_date = datetime.fromisoformat(date_str).date()
            except ValueError:
                return jsonify({"error": "date must be YYYY-MM-DD"}), 400
            if r.start_time.date() != target_date:
                continue

        result.append(
            {
                "reservation_id": r.reservation_id,
                "user_id": r.user_id,
                "room_id": r.room_id,
                "start_time": r.start_time.isoformat(),
                "end_time": r.end_time.isoformat(),
                "status": r.status.value,
            }
        )

    return jsonify(result)


@app.route("/api/reservations", methods=["POST"])
def api_create_reservation():
    data = request.get_json(force=True) or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    # ここで BAN チェック
    now = now_jst()
    summary = penalty_service.get_summary(user_id, now)
    if summary["is_banned"]:
        return (
            jsonify(
                {
                    "error": "user is banned",
                    "points": summary["points"],
                    "threshold": summary["threshold"],
                    "ban_until": summary["ban_until"],
                    "window_days": summary["window_days"],
                }
            ),
            403,
        )

    room_id = data.get("room_id", ROOM_ID)

    date_str = data.get("date")  # "2025-11-29"
    start_hm = data.get("start_time")  # "15:00"
    end_hm = data.get("end_time")  # "15:30"
    if not date_str or not start_hm or not end_hm:
        return jsonify({"error": "date, start_time, end_time are required"}), 400

    # ISO8601(+09:00) に組み立てる
    try:
        start_iso = f"{date_str}T{start_hm}:00+09:00"
        end_iso = f"{date_str}T{end_hm}:00+09:00"
        start_dt = parse_jst_datetime(start_iso)
        end_dt = parse_jst_datetime(end_iso)
    except Exception:
        return jsonify({"error": "invalid date/time format"}), 400

    # 開始・終了の整合性
    if end_dt <= start_dt:
        return jsonify({"error": "end_time must be after start_time"}), 400

    # ===== ここから追加のビジネスルール =====

    # (1) 過去時刻への予約は禁止
    #     now_jst() は time_utils 経由で「シミュレートされた現在時刻」を返すので、
    #     デバッグモードの時間加速にも自動対応します。
    if start_dt < now:
        return jsonify({"error": "過去の時刻には予約できません"}), 400

    # (2) 予約時間の長さ（分）の最小・最大
    duration_minutes = (end_dt - start_dt).total_seconds() / 60.0
    if duration_minutes < MIN_RESERVE_MINUTES:
        return (
            jsonify(
                {"error": f"予約時間は最低 {MIN_RESERVE_MINUTES} 分以上にしてください"}
            ),
            400,
        )
    if duration_minutes > MAX_RESERVE_MINUTES:
        return (
            jsonify({"error": f"予約時間は最大 {MAX_RESERVE_MINUTES} 分までです"}),
            400,
        )

    # (3) あまりにも先の日付への予約を禁止
    latest_allowed = now + timedelta(days=MAX_RESERVE_DAYS_AHEAD)
    if start_dt.date() > latest_allowed.date():
        return (
            jsonify(
                {"error": f"予約は {MAX_RESERVE_DAYS_AHEAD} 日先までしかできません"}
            ),
            400,
        )

    # ===== ここまで追加のビジネスルール =====

    try:
        res = reservation_repo.create_reservation(
            room_id=room_id,
            user_id=user_id,
            start_time=start_dt,
            end_time=end_dt,
        )
    except ValueError as e:
        # 重複、バッファ違反など
        return jsonify({"error": str(e)}), 400

    return (
        jsonify(
            {
                "reservation_id": res.reservation_id,
                "user_id": res.user_id,
                "room_id": res.room_id,
                "start_time": res.start_time.isoformat(),
                "end_time": res.end_time.isoformat(),
                "status": res.status.value,
            }
        ),
        201,
    )


@app.route("/api/reservations/<reservation_id>", methods=["DELETE"])
def api_cancel_reservation(reservation_id: str):
    ok = reservation_repo.cancel_reservation(reservation_id)
    if not ok:
        return jsonify({"error": "reservation not found"}), 404
    return jsonify({"status": "cancelled", "reservation_id": reservation_id})


@app.route("/api/penalties/<user_id>", methods=["GET"])
def api_penalty_summary(user_id: str):
    now = now_jst()
    summary = penalty_service.get_summary(user_id, now)
    return jsonify(summary)


@app.route("/ping")
def ping():
    return "pong"


if __name__ == "__main__":
    occupancy_provider = create_occupancy_provider(ai_camera_repository)

    t = threading.Thread(target=background_monitoring_task, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=8000, debug=False)
