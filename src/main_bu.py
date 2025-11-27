from flask import Flask, jsonify, request, render_template_string, redirect
import Repository
import os, json, time, threading
import webbrowser
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import traceback
import requests
import random  # ← 追加

# ============= 基本セットアップ =============
load_dotenv()  # .env が無くてもOK（全てデフォルトあり）
app = Flask(__name__)

# 既存の AiCameraRepository をそのまま利用
ai = Repository.AiCameraRepository(
    console_endpoint=os.getenv("CONSOLE_ENDPOINT"),
    auth_endpoint=os.getenv("AUTH_ENDPOINT"),
    client_id=os.getenv("CLIENT_ID"),
    client_secret=os.getenv("CLIENT_SECRET"),
    device_id=os.getenv("DEVICE_ID"),
)

# --- 日本時間（JST）設定: 端末アクセスログや time.localtime ベースの表示もJSTにする ---
os.environ.setdefault("TZ", "Asia/Tokyo")
try:
    time.tzset()  # Linux/Unix系で有効（Windowsは無視される）
except Exception:
    pass

# JST タイムゾーンとISO文字列ヘルパ
JST = timezone(timedelta(hours=9))
def now_iso_jst() -> str:
    return datetime.now(JST).isoformat()

# ============= 運用パラメータ（デフォルトあり／後で上書き可） =============
ROOM_ID = os.getenv("ROOM_ID", "R-0001")
POLL_FPS = float(os.getenv("POLL_FPS", "2.0"))           # 推論ポーリングFPS
LINE_AXIS = os.getenv("LINE_AXIS", "y")                  # 'y' or 'x'（水平or垂直ライン）
LINE_POS = int(os.getenv("LINE_POS", "360"))             # 画面上のライン位置（px相当）
DIR_POSITIVE_IS_ENTER = (os.getenv("DIR_POSITIVE_IS_ENTER", "1") == "1")  # 進行正方向=入室
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.6"))         # 検出信頼度フィルタ
MIN_WH = int(os.getenv("MIN_WH", "20"))                  # 最小サイズフィルタ（小物・誤検知除去）
IOU_MATCH = float(os.getenv("IOU_MATCH", "0.3"))         # 擬似トラッキングのIoUしきい
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.9"))  # 最終イベント採用しきい
MOCK_RESERVATION_MATCH = (os.getenv("MOCK_RESERVATION_MATCH", "1") == "1")  # 予約API未配線でもOK
RESERVATION_API_BASE = os.getenv("RESERVATION_API_BASE")  # 例: https://api.example.com
RESERVATION_API_KEY  = os.getenv("RESERVATION_API_KEY")   # あるなら
LOCK_API_BASE        = os.getenv("LOCK_API_BASE")         # 例: https://lock.example.com
LOCK_API_KEY         = os.getenv("LOCK_API_KEY")          # あるなら
# 共有LineCounter（モック/実機の両方で同じトラッキング状態を使う）
LC = None
def _make_line_counter():
    return LineCounter(
        axis=LINE_AXIS, pos=LINE_POS,
        dir_positive_is_enter=DIR_POSITIVE_IS_ENTER,
        iou_match=IOU_MATCH, min_score=MIN_SCORE, min_wh=MIN_WH
    )
DISABLE_POLLER = (os.getenv("DISABLE_POLLER","0") == "1")
# --- KPI/AB関連 ---
AB_BUCKET = os.getenv("AB_BUCKET", "treatment")
KPI_LOG_PATH = os.getenv("KPI_LOG_PATH")  # 例: /var/log/kpi_events.jsonl
# --- 予約許容窓（将来: 予約API側の判定結果と突合する用） ---
ARRIVAL_WINDOW_BEFORE_MIN = int(os.getenv("ARRIVAL_WINDOW_BEFORE_MIN", "10"))
ARRIVAL_WINDOW_AFTER_MIN  = int(os.getenv("ARRIVAL_WINDOW_AFTER_MIN", "15"))
NO_SHOW_GRACE_MIN         = int(os.getenv("NO_SHOW_GRACE_MIN", "7"))
MOCK_QR_REQUIRE_ID = (os.getenv("MOCK_QR_REQUIRE_ID", "0") == "1")
# --- 予約終了処理の窓・猶予（分） ---
END_CLOSE_WINDOW_MIN = int(os.getenv("END_CLOSE_WINDOW_MIN", "5"))     # 終了±5分で退出検知→自動クローズ
OVERSTAY_GRACE_MIN   = int(os.getenv("OVERSTAY_GRACE_MIN", "5"))       # 終了+5分を超えて在室>0 → 超過利用

# ============= ユーティリティ（外部依存なし） =============
def _center(b): return ((b["X"] + b["x"]) / 2.0, (b["Y"] + b["y"]) / 2.0)

def _iou(a, b):
    x1 = max(a["X"], b["X"]); y1 = max(a["Y"], b["Y"])
    x2 = min(a["x"], b["x"]); y2 = min(a["y"], b["y"])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area = lambda bb: (bb["x"] - bb["X"]) * (bb["y"] - bb["Y"])
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0

def log_event(name: str, **payload):
    rec = {"ts": now_iso_jst(), "event": name, **payload}
    print(json.dumps(rec, ensure_ascii=False), flush=True)

# --- 占有人数とメトリクス（スレッド安全） ---
_O_LOCK = threading.Lock()
_OCCUPANCY = 0
_METRICS = {"enter": 0, "exit": 0, "filtered": 0, "checkin_success": 0, "checkin_fail": 0}

def _snapshot_metrics():
    """現在の occupancy / counters / bucket をコピーして返す（シナリオサマリ用）"""
    with _O_LOCK:
        return {
            "occupancy": _OCCUPANCY,
            "counters": dict(_METRICS),
            "bucket": AB_BUCKET,
        }

def _metrics_delta(after: dict, before: dict):
    """before→after の差分を counters ごとに計算"""
    da = {}
    ba = before.get("counters", {})
    aa = after.get("counters", {})
    for k, v in aa.items():
        da[k] = v - ba.get(k, 0)
    da["occupancy_delta"] = after.get("occupancy", 0) - before.get("occupancy", 0)
    return da

# --- No-show watcher state (最小) ---
_RSV_STATE = {}  # {reservation_id: {"start": datetime, "end": datetime, "checked_in": bool, "auto_released": bool}}
_RS_LOCK = threading.Lock()

def _rsv_upsert(reservation_id, start_ts, end_ts):
    with _RS_LOCK:
        _RSV_STATE.setdefault(reservation_id, {
            "start": datetime.fromisoformat(start_ts) if isinstance(start_ts, str) else start_ts,
            "end":   datetime.fromisoformat(end_ts)   if isinstance(end_ts, str)   else end_ts,
            "checked_in": False,
            "auto_released": False,
            # 追加
            "closed": False,        # 終了処理でクローズ済み
            "overstayed": False     # 超過利用を記録済み
        })

def _record_checkin(reservation_id):
    if not reservation_id:
        return
    with _RS_LOCK:
        st = _RSV_STATE.get(reservation_id)
        if st:
            st["checked_in"] = True

def _kpi_write(rec: dict):
    path = KPI_LOG_PATH
    if not path:
        return
    try:
        # 親ディレクトリが無ければ作成（"." は無視）
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        log_event("kpi_write_error", error=str(e), path=path)

# ============= 簡易ラインカウンタ（擬似トラッキング付き） =============
class LineCounter:
    """
    AiCameraRepository の bbox辞書（{"1":{P,X,Y,x,y,C}, ...}）を入力に、
    IoUで擬似ID継続→指定ラインを跨いだら enter/exit を返す。
    """
    def __init__(self, axis="y", pos=360, dir_positive_is_enter=True,
                 iou_match=0.3, min_score=0.6, min_wh=20):
        self.axis = axis
        self.pos = pos
        self.sign = 1 if dir_positive_is_enter else -1
        self.iou_match = iou_match
        self.min_score = min_score
        self.min_wh = min_wh
        self.tracks = {}     # id -> {"bbox":..., "last_center":(x,y)}
        self._next_id = 1

    def update(self, det: dict):
        # 1) 前処理：スコア・サイズフィルタ
        boxes = []
        for v in (det or {}).values():
            if not isinstance(v, dict) or "P" not in v: continue
            if v["P"] < self.min_score: continue
            if (v["x"] - v["X"] < self.min_wh) or (v["y"] - v["Y"] < self.min_wh): continue
            boxes.append(v)

        # 2) 既存トラックにIoUで割当
        assigned = set()
        events = []
        for tid, tr in list(self.tracks.items()):
            best_idx, best_box, best_iou = None, None, self.iou_match
            for i, b in enumerate(boxes):
                if i in assigned: continue
                iou = _iou(tr["bbox"], b)
                if iou > best_iou:
                    best_idx, best_box, best_iou = i, b, iou
            if best_box is not None:
                assigned.add(best_idx)
                cx, cy = _center(best_box)
                pcx, pcy = tr["last_center"]
                crossed = False
                if self.axis == "y":
                    if (pcy - self.pos) * (cy - self.pos) < 0:
                        crossed = True
                        typ = "enter" if (cy - pcy) * self.sign > 0 else "exit"
                else:
                    if (pcx - self.pos) * (cx - self.pos) < 0:
                        crossed = True
                        typ = "enter" if (cx - pcx) * self.sign > 0 else "exit"
                self.tracks[tid] = {"bbox": best_box, "last_center": (cx, cy)}
                if crossed:
                    events.append({"type": typ, "confidence": float(best_box["P"])})
            else:
                # 見失い
                self.tracks.pop(tid, None)

        # 3) 未割当は新規トラック
        for i, b in enumerate(boxes):
            if i in assigned: continue
            cx, cy = _center(b)
            self.tracks[self._next_id] = {"bbox": b, "last_center": (cx, cy)}
            self._next_id += 1

        return events

def grant_lock(room_id: str) -> bool:
    # 鍵APIが設定されていなければモックで通す
    if not LOCK_API_BASE:
        log_event("lock_grant_mock", room_id=room_id)
        return True
    try:
        url = f"{LOCK_API_BASE}/rooms/{room_id}/grant"
        headers = {"Authorization": f"Bearer {LOCK_API_KEY}"} if LOCK_API_KEY else {}
        r = requests.post(url, json={}, headers=headers, timeout=3)
        ok = (200 <= r.status_code < 300)
        log_event("lock_grant_call", room_id=room_id, status=r.status_code, ok=ok)
        return ok
    except Exception as e:
        log_event("lock_grant_error", room_id=room_id, error=str(e))
        return False

# ============= 予約チェックイン（Stub：あとで実APIに差し替え） =============
def checkin_attempt(room_id: str, ts_iso: str):
    """
    予約APIが設定されていればHTTPで試行。
    未設定ならモックで“checked_in”を出して先へ進む。
    """
    # --- AB: control バケットは常に no-hit（サーバA/BではなくクライアントA/Bで即比較できる）
    if AB_BUCKET.lower().startswith("control"):
        log_event("ab_control_skip", room_id=room_id, ts=ts_iso)
        return None

    # 本番ルート
    if RESERVATION_API_BASE:
        try:
            url = f"{RESERVATION_API_BASE}/reservations/checkin-attempt"
            headers = {"Authorization": f"Bearer {RESERVATION_API_KEY}"} if RESERVATION_API_KEY else {}
            payload = {"room_id": room_id, "ts": ts_iso}
            r = requests.post(url, json=payload, headers=headers, timeout=3)
            data = r.json() if r.headers.get("Content-Type","").startswith("application/json") else {}
            matched = bool(data.get("matched")) if r.ok else False
            rid = data.get("reservation_id")
            log_event("rsv_checkin_call", room_id=room_id, status=r.status_code, matched=matched, rid=rid)
            if matched and rid:
                # これを追加
                _record_checkin(rid)
                # 鍵付与はベストエフォート（失敗してもログして継続）
                grant_lock(room_id)
                return rid
            else:
                log_event("checkin_attempt_nohit", room_id=room_id, ts=ts_iso)
                return None
        except Exception as e:
            log_event("rsv_checkin_error", room_id=room_id, error=str(e))
            # エラー時は“安全側”でモックに倒して先へ進めるかは運用方針次第。ここでは倒さない。
            return None

    # モック（従来）
    if not MOCK_RESERVATION_MATCH:
        log_event("checkin_attempt_nohit", room_id=room_id, ts=ts_iso)
        return None
    rid = f"rsv_mock_{int(time.time())}"
    log_event("checked_in", room_id=room_id, reservation_id=rid, ts=ts_iso)
    # モックでも鍵付与を叩いておくと配線確認が楽
    grant_lock(room_id)
    return rid

def process_event(room_id: str, ev_type: str, ts_iso: str, count_delta: int, confidence: float):
    """
    ライン跨ぎイベントを処理：
      - 占有人数を更新
      - enter時はチェックイン試行（予約API or モック）
      - KPIをstdoutとjsonl（任意）に記録
    """
    global _OCCUPANCY  # ★ここを先頭に

    # deltaはイベント種別で決定（引数のcount_deltaは無視）
    delta = 1 if ev_type == "enter" else (-1 if ev_type == "exit" else 0)

    # 低信頼度はフィルタ
    if confidence < CONFIDENCE_MIN:
        with _O_LOCK:
            _METRICS["filtered"] += 1
            occ = _OCCUPANCY  # 参照してOK（上でglobal宣言済）
        rec = {"ts": ts_iso, "event": "filtered", "room_id": room_id, "bucket": AB_BUCKET,
               "confidence": confidence, "occupancy": occ}
        log_event("filtered", room_id=room_id, conf=confidence)
        _kpi_write(rec)
        return

    # 占有人数更新（下限0にクリップ）
    rid = None
    with _O_LOCK:
        _OCCUPANCY = max(0, _OCCUPANCY + delta)
        occ_after = _OCCUPANCY
        if ev_type in ("enter", "exit"):
            _METRICS[ev_type] += 1

    # enterならチェックイン（A/B評価の肝）
    matched = None
    if ev_type == "enter":
        rid = checkin_attempt(room_id, ts_iso)
        matched = bool(rid)
        with _O_LOCK:
            if matched:
                _METRICS["checkin_success"] += 1
            else:
                _METRICS["checkin_fail"] += 1
    else:
        # exitはKPI上は通過イベントのみ
        log_event("edge_event", room_id=room_id, type=ev_type, ts=ts_iso,
                  count_delta=delta, confidence=confidence)

    # KPI行をjsonlへ（任意）
    kpi = {
        "ts": ts_iso,
        "room_id": room_id,
        "bucket": AB_BUCKET,
        "event": ev_type,
        "confidence": confidence,
        "delta": delta,
        "occupancy": occ_after,
    }
    if matched is not None:
        kpi["matched"] = matched
        if rid:
            kpi["reservation_id"] = rid
        kpi["arrival_window_before_min"] = ARRIVAL_WINDOW_BEFORE_MIN
        kpi["arrival_window_after_min"] = ARRIVAL_WINDOW_AFTER_MIN
        kpi["no_show_grace_min"] = NO_SHOW_GRACE_MIN

    _kpi_write(kpi)

def _normalize_to_repo_dict(payload: dict) -> dict:
    """
    受け取ったJSONを Repository.AiCameraRepository 相当の形式
    {"1":{"X":l,"Y":t,"x":r,"y":b,"P":score}, ...} に変換する。
    対応フォーマット例：
      A) {"1":{"X":..,"Y":..,"x":..,"y":..,"P":..}, "2":{...}}
      B) {"boxes":[{"cx":..,"cy":..,"w":..,"h":..,"score":..}, ...], "frame_w":..., "frame_h":...}
      C) {"boxes":[{"x":..,"y":..,"width":..,"height":..,"score":..}, ...]}  # 左上(x,y)+幅高
      D) 正規化座標(0-1)は frame_w/frame_h があればピクセル換算
    """
    if not payload:
        return {}

    # すでに「値が dict」で、その中に X を含む形式ならそのまま（キーは文字列化）
    if isinstance(payload, dict) and any(isinstance(v, dict) and ("X" in v) for v in payload.values()):
        return {str(k): v for k, v in payload.items()}

    boxes = payload.get("boxes")
    if not isinstance(boxes, list):
        return {}

    fw = float(payload.get("frame_w", 1.0))
    fh = float(payload.get("frame_h", 1.0))
    out = {}
    for i, b in enumerate(boxes, start=1):
        if not isinstance(b, dict):
            continue

        # スコア
        P = float(b.get("score", b.get("P", 1.0)))

        # 形式B: 中心点+幅高（cx,cy,w,h）
        if {"cx","cy","w","h"} <= b.keys():
            cx = float(b["cx"]); cy = float(b["cy"])
            w  = float(b["w"]);  h  = float(b["h"])
            # 正規化とみなすケース（0-1っぽい数値）を簡易対応
            if 0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0 and (fw>1 or fh>1):
                cx *= fw; cy *= fh; w *= fw; h *= fh
            l = cx - w/2.0; t = cy - h/2.0; r = cx + w/2.0; btm = cy + h/2.0
            out[str(i)] = {"X": l, "Y": t, "x": r, "y": btm, "P": P}
            continue

        # 形式C: 左上+幅高（x,y,width,height）
        if {"x","y","width","height"} <= b.keys():
            l = float(b["x"]); t = float(b["y"])
            w = float(b["width"]); h = float(b["height"])
            # 正規化対応
            if 0.0 <= l <= 1.0 and 0.0 <= t <= 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0 and (fw>1 or fh>1):
                l *= fw; t *= fh; w *= fw; h *= fh
            out[str(i)] = {"X": l, "Y": t, "x": l+w, "y": t+h, "P": P}
            continue

        # 既に left/top/right/bottom系
        if {"left","top","right","bottom"} <= b.keys():
            out[str(i)] = {"X": float(b["left"]), "Y": float(b["top"]),
                           "x": float(b["right"]), "y": float(b["bottom"]), "P": P}
            continue

    return out

# ============= 推論ポーラ（スレッド） =============
def edge_poller_loop():
    global LC
    if LC is None:
        LC = _make_line_counter()
    period = max(0.1, 1.0 / POLL_FPS)
    log_event("poller_started", room_id=ROOM_ID, fps=POLL_FPS,
              axis=LINE_AXIS, pos=LINE_POS)
    while True:
        try:
            det = ai.fetch_inference_result() or {}
            for ev in LC.update(det):
                ts = now_iso_jst()
                process_event(ROOM_ID, ev["type"], ts, 0, ev["confidence"])
            time.sleep(period)
        except Exception as e:
            # 例外でループが死なないようにする（ログ＋短い待機で再開）
            log_event("poller_error", room_id=ROOM_ID, error=str(e))
            print(traceback.format_exc(), flush=True)
            time.sleep(1.0)
            continue

EDGE_SHARED_SECRET = os.getenv("EDGE_SHARED_SECRET")

def _require_edge_secret():
    if not EDGE_SHARED_SECRET:
        return None  # 無効（誰でもOK）
    if request.headers.get("X-Edge-Secret") != EDGE_SHARED_SECRET:
        return jsonify(ok=False, error="forbidden"), 403
    return None

# ============= HTTP Routes（既存＋最小デバッグ） =============
@app.get("/ping")
def ping(): return "pong"

@app.get("/healthz")
def healthz():
    return jsonify(ok=True)

@app.get("/readyz")
def readyz():
    # 必須envが埋まっているかの簡易チェック（ネットワーク疎通までは行わない）
    ok = all([
        os.getenv("CONSOLE_ENDPOINT"),
        os.getenv("AUTH_ENDPOINT"),
        os.getenv("CLIENT_ID"),
        os.getenv("CLIENT_SECRET"),
        os.getenv("DEVICE_ID"),
    ])
    return jsonify(ready=bool(ok))

@app.get("/config")
def get_config():
    return jsonify({
        "room_id": ROOM_ID,
        "poll_fps": POLL_FPS,
        "line_axis": LINE_AXIS,
        "line_pos": LINE_POS,
        "dir_positive_is_enter": DIR_POSITIVE_IS_ENTER,
        "min_score": MIN_SCORE,
        "min_wh": MIN_WH,
        "iou_match": IOU_MATCH,
        "confidence_min": CONFIDENCE_MIN,
        "mock_reservation_match": MOCK_RESERVATION_MATCH,
    })

@app.get("/")
def root():
    # ブラウザ（text/html）なら UI へ、API用途（JSONクライアント）なら ?raw=1 で素のJSON
    if "text/html" in request.headers.get("Accept", "") and request.args.get("raw") != "1":
        return redirect("/ui", code=302)
    return ai.fetch_inference_result()

@app.get("/debug/last-inference")
def debug_last():
    try:
        return jsonify({"ok": True, "inference": ai.fetch_inference_result() or {}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/ab/set")
def ab_set():
    global AB_BUCKET
    data = request.get_json(silent=True) or {}
    b = str(data.get("bucket", "")).strip().lower()
    if b not in ("control","treatment"):
        return jsonify(ok=False, error="bucket must be control or treatment"), 400
    AB_BUCKET = b
    return jsonify(ok=True, bucket=AB_BUCKET)

@app.post("/config/update")
def config_update():
    global LINE_AXIS, LINE_POS, DIR_POSITIVE_IS_ENTER
    global MIN_SCORE, MIN_WH, IOU_MATCH, CONFIDENCE_MIN
    global ARRIVAL_WINDOW_BEFORE_MIN, ARRIVAL_WINDOW_AFTER_MIN, NO_SHOW_GRACE_MIN
    global LC

    p = request.get_json(force=True) or {}
    # セーフに型変換
    LINE_AXIS = p.get("line_axis", LINE_AXIS) if p.get("line_axis") in ("x","y") else LINE_AXIS
    LINE_POS  = int(p.get("line_pos", LINE_POS))
    DIR_POSITIVE_IS_ENTER = bool(int(p.get("dir_positive_is_enter", int(DIR_POSITIVE_IS_ENTER))))
    MIN_SCORE = float(p.get("min_score", MIN_SCORE))
    MIN_WH    = int(p.get("min_wh", MIN_WH))
    IOU_MATCH = float(p.get("iou_match", IOU_MATCH))
    CONFIDENCE_MIN = float(p.get("confidence_min", CONFIDENCE_MIN))
    ARRIVAL_WINDOW_BEFORE_MIN = int(p.get("arrival_before_min", ARRIVAL_WINDOW_BEFORE_MIN))
    ARRIVAL_WINDOW_AFTER_MIN  = int(p.get("arrival_after_min", ARRIVAL_WINDOW_AFTER_MIN))
    NO_SHOW_GRACE_MIN         = int(p.get("no_show_grace_min", NO_SHOW_GRACE_MIN))

    # LineCounter を新条件で作り直し
    LC = _make_line_counter()
    return jsonify(ok=True)

@app.post("/simulate/enter")
def simulate_enter():
    guard = _require_edge_secret()
    if guard is not None:
        return guard

    ts = now_iso_jst()
    process_event(ROOM_ID, "enter", ts, 0, confidence=0.95)
    return jsonify(ok=True, ts=ts)

@app.post("/simulate/frame")
def simulate_frame():
    """
    任意の検出フレーム（AITRIOS風/汎用）をPOSTで注入→ライン通過検出→イベント発火。
    例A: 既に正規化済み
      {
        "1":{"X":100,"Y":200,"x":160,"y":320,"P":0.95},
        "2":{"X":250,"Y":210,"x":300,"y":340,"P":0.90}
      }
    例B: AITRIOS風（中心+幅高, 0-1正規化、フレームサイズ指定）
      {
        "frame_w":1280, "frame_h":720,
        "boxes":[{"cx":0.49,"cy":0.55,"w":0.05,"h":0.12,"score":0.96}]
      }
    """
    guard = _require_edge_secret()
    if guard is not None:
        return guard

    global LC
    if LC is None:
        LC = _make_line_counter()

    payload = request.get_json(force=True)
    det = _normalize_to_repo_dict(payload)
    ts = now_iso_jst()
    events = []
    for ev in LC.update(det):
        process_event(ROOM_ID, ev["type"], ts, 0, ev["confidence"])
        events.append(ev)
    return jsonify(ok=True, events=events, ts=ts)

@app.post("/simulate/sweep")
def simulate_sweep():
    """
    1人がラインを跨ぐ連続フレームを自動生成して注入。
    body 例: {"from":320, "to":400, "axis":"y", "steps":10,
              "w":60, "h":120, "cx":640, "score":0.95, "frame_w":1280, "frame_h":720}
    省略時デフォルト: axis=LINE_AXIS, 中心x=640, w=60, h=120, steps=10, score=0.95
    """
    guard = _require_edge_secret()
    if guard is not None:
        return guard

    global LC
    if LC is None:
        LC = _make_line_counter()

    p = request.get_json(silent=True) or {}
    axis  = p.get("axis", LINE_AXIS)
    v0    = float(p.get("from", LINE_POS - 40))
    v1    = float(p.get("to",   LINE_POS + 40))
    steps = int(p.get("steps", 10))
    w     = float(p.get("w", 60.0))
    h     = float(p.get("h", 120.0))
    cx    = float(p.get("cx", 640.0))
    score = float(p.get("score", 0.95))

    events = []
    for i in range(steps):
        t = i / max(1, steps-1)
        pos = v0 + (v1 - v0) * t
        if axis == "y":
            cy = pos
            det = {"1": {"X": cx - w/2, "Y": cy - h/2, "x": cx + w/2, "y": cy + h/2, "P": score}}
        else:
            cx2 = pos; cy = p.get("cy", 360.0)
            det = {"1": {"X": cx2 - w/2, "Y": cy - h/2, "x": cx2 + w/2, "y": cy + h/2, "P": score}}

        ts = now_iso_jst()
        for ev in LC.update(det):
            process_event(ROOM_ID, ev["type"], ts, 0, ev["confidence"])
            events.append(ev)
        time.sleep(0.02)  # 20ms間隔で擬似連続

    return jsonify(ok=True, events=events, steps=steps)

def _simulate_visit_once(params: dict):
    """
    1人分の来訪（入室→滞在→退室）をシミュレートする内部ヘルパー。
    /simulate/visit, /simulate/scenario から共通利用。
    """
    global LC
    if LC is None:
        LC = _make_line_counter()

    p = params or {}
    axis  = p.get("axis", LINE_AXIS)
    steps = int(p.get("steps", 12))
    v0    = float(p.get("from", LINE_POS - 40))
    v1    = float(p.get("to",   LINE_POS + 40))
    w     = float(p.get("w", 60.0))
    h     = float(p.get("h", 120.0))
    cx    = float(p.get("cx", 640.0))
    cy    = float(p.get("cy", 360.0))
    score = float(p.get("score", 0.95))
    dwell_ms = int(p.get("dwell_ms", 500))

    def _sweep(v_from, v_to):
        evs = []
        for i in range(steps):
            t = i / max(1, steps - 1)
            pos = v_from + (v_to - v_from) * t
            if axis == "y":
                cy2 = pos
                cx2 = cx
            else:
                cx2 = pos
                cy2 = cy
            det = {
                "1": {
                    "X": cx2 - w / 2,
                    "Y": cy2 - h / 2,
                    "x": cx2 + w / 2,
                    "y": cy2 + h / 2,
                    "P": score,
                }
            }
            ts = now_iso_jst()
            for ev in LC.update(det):
                process_event(ROOM_ID, ev["type"], ts, 0, ev["confidence"])
                evs.append(ev)
            time.sleep(0.02)
        return evs

    events = []
    # 入室スイープ
    events += _sweep(v0, v1)
    # 滞在
    time.sleep(max(0, dwell_ms) / 1000.0)
    # 退室スイープ（逆方向）
    events += _sweep(v1, v0)

    return events, steps, dwell_ms

def _simulate_two_way_cross(params: dict):
    """
    2人が反対側から同時にラインを跨ぐ“すれ違い”シナリオ。
    /simulate/scenario から pattern="cross" で呼び出す。
    """
    global LC
    if LC is None:
        LC = _make_line_counter()

    p = params or {}
    axis  = p.get("axis", LINE_AXIS)
    steps = int(p.get("steps", 12))
    v0    = float(p.get("from", LINE_POS - 60))  # ラインより手前
    v1    = float(p.get("to",   LINE_POS + 60))  # ラインより奥
    w     = float(p.get("w", 60.0))
    h     = float(p.get("h", 120.0))
    # 横方向の位置を少しずらす（IoUで同一人物としてマージされにくくする）
    cx1   = float(p.get("cx1", 600.0))
    cx2   = float(p.get("cx2", 680.0))
    cy    = float(p.get("cy", 360.0))
    score = float(p.get("score", 0.95))

    def _frame(pos1, pos2):
        if axis == "y":
            y1 = pos1
            y2 = pos2
            det = {
                "1": {"X": cx1 - w/2, "Y": y1 - h/2, "x": cx1 + w/2, "y": y1 + h/2, "P": score},
                "2": {"X": cx2 - w/2, "Y": y2 - h/2, "x": cx2 + w/2, "y": y2 + h/2, "P": score},
            }
        else:
            x1 = pos1
            x2 = pos2
            det = {
                "1": {"X": x1 - w/2, "Y": cy - h/2, "x": x1 + w/2, "y": cy + h/2, "P": score},
                "2": {"X": x2 - w/2, "Y": cy - h/2, "x": x2 + w/2, "y": cy + h/2, "P": score},
            }
        return det

    events = []
    for i in range(steps):
        t = i / max(1, steps - 1)
        # 人1: v0 → v1（入室方向）、人2: v1 → v0（退室方向）という想定
        pos1 = v0 + (v1 - v0) * t
        pos2 = v1 - (v1 - v0) * t
        det = _frame(pos1, pos2)
        ts = now_iso_jst()
        for ev in LC.update(det):
            process_event(ROOM_ID, ev["type"], ts, 0, ev["confidence"])
            events.append(ev)
        time.sleep(0.02)

    # サマリ用に enter/exit を数えて返す
    cnt = {"enter": 0, "exit": 0}
    for ev in events:
        t = ev.get("type")
        if t in cnt:
            cnt[t] += 1

    return {
        "pattern": "cross",
        "visitors": 2,
        "events": events,
        "events_count": cnt,
        "steps": steps,
    }

@app.post("/simulate/visit")
def simulate_visit():
    """
    1人分の来訪を自動生成（入室→滞在→退室）。
    body 例:
      {"axis":"y","steps":12,"from":320,"to":400,"dwell_ms":800,
       "w":60,"h":120,"cx":640,"cy":360,"score":0.95}
    """
    p = request.get_json(silent=True) or {}

    # シナリオ前後のメトリクスを比較するためにスナップショットを取る
    before = _snapshot_metrics()
    events, steps, dwell_ms = _simulate_visit_once(p)
    after = _snapshot_metrics()

    # イベント種別ごとの集計
    ev_count = {"enter": 0, "exit": 0}
    for ev in events:
        t = ev.get("type")
        if t in ev_count:
            ev_count[t] += 1

    summary = {
        "bucket": AB_BUCKET,
        "visitors": 1,
        "events_count": ev_count,
        "metrics_before": before,
        "metrics_after": after,
        "metrics_delta": _metrics_delta(after, before),
    }

    return jsonify(
        ok=True,
        visitors=1,
        events=events,
        steps=steps,
        dwell_ms=dwell_ms,
        summary=summary,
    )

@app.post("/simulate/scenario")
def simulate_scenario():
    """
    複数人の来訪シナリオをまとめて再生するエンドポイント。
    body 例（省略可・デフォルトあり）:
      {
        "visitors": 5,
        "min_dwell_ms": 400,
        "max_dwell_ms": 1500,
        "gap_ms": 500,
        "jitter_ms": 200,
        "axis": "y",
        "from": 320,
        "to": 400,
        "steps": 12,
        "w": 60,
        "h": 120,
        "cx": 640,
        "cy": 360,
        "score": 0.95,
        "pattern": "staggered"   # or "cross"
      }
    """
    p = request.get_json(silent=True) or {}
    pattern = str(p.get("pattern", "staggered")).lower()

    before = _snapshot_metrics()

    all_items = []
    total_ev_count = {"enter": 0, "exit": 0}

    if pattern == "cross":
        # 2人すれ違いパターン（1セット）
        res = _simulate_two_way_cross(p)
        all_items.append(res)
        for t, n in res.get("events_count", {}).items():
            if t in total_ev_count:
                total_ev_count[t] += n

        visitors = res.get("visitors", 2)
        gap_ms = int(p.get("gap_ms", 0))
        min_d = int(p.get("min_dwell_ms", p.get("dwell_ms", 0)))
        max_d = min_d
    else:
        # 従来どおりのランダム来訪シナリオ
        visitors = int(p.get("visitors", p.get("num_visitors", 3)))
        visitors = max(1, visitors)

        gap_ms   = int(p.get("gap_ms", 500))
        jitter_ms = int(p.get("jitter_ms", 0))

        base_dwell = int(p.get("dwell_ms", 500))
        min_d = int(p.get("min_dwell_ms", base_dwell))
        max_d = int(p.get("max_dwell_ms", min_d))
        if max_d < min_d:
            max_d = min_d

        for i in range(visitors):
            vp = dict(p)
            dwell_ms = random.randint(min_d, max_d) if max_d > min_d else min_d
            vp["dwell_ms"] = dwell_ms

            events, steps, used_dwell = _simulate_visit_once(vp)

            # visitorごとの enter/exit 集計
            ev_count = {"enter": 0, "exit": 0}
            for ev in events:
                t = ev.get("type")
                if t in ev_count:
                    ev_count[t] += 1
                    total_ev_count[t] += 1

            all_items.append({
                "visitor_index": i,
                "dwell_ms": used_dwell,
                "num_events": len(events),
                "events": events,
                "events_count": ev_count,
            })

            # 次の人の開始まで待つ（最後の人の後は待たない）
            if i != visitors - 1:
                delay = gap_ms
                if jitter_ms:
                    delay += random.randint(-jitter_ms, jitter_ms)
                time.sleep(max(0, delay) / 1000.0)

    after = _snapshot_metrics()

    summary = {
        "bucket": AB_BUCKET,
        "pattern": pattern,
        "visitors": visitors,
        "events_count": total_ev_count,
        "metrics_before": before,
        "metrics_after": after,
        "metrics_delta": _metrics_delta(after, before),
    }

    return jsonify(
        ok=True,
        visitors=visitors,
        gap_ms=gap_ms,
        min_dwell_ms=min_d,
        max_dwell_ms=max_d,
        pattern=pattern,
        summary=summary,
        items=all_items,
    )

@app.post("/simulate/reservation_scenario")
def simulate_reservation_scenario():
    """
    予約中心のノーショー検証用シナリオ。
    body 例:
      {
        "pattern": "no_show",
        "num_reservations": 3,
        "duration_min": 30,
        "start_offset_min": -30,
        "id_prefix": "sim_ns_"
      }

    - 予約を num_reservations 件自動生成
    - それぞれに対して no_show_detected KPI イベントを1行ずつ書き込む
    - カメラの入退室イベントや在室人数は変えない（純粋に「ノーショーだけ」を見るため）
    """
    p = request.get_json(silent=True) or {}
    pattern = str(p.get("pattern", "no_show")).lower()

    # 今は no_show のみサポート（将来 on_time/mixed を足す想定）
    if pattern not in ("no_show",):
        return jsonify(ok=False, error="unsupported pattern; now only 'no_show' is supported"), 400

    num = int(p.get("num_reservations", p.get("count", 3)))
    if num < 1:
        num = 1

    duration_min = int(p.get("duration_min", 30))

    # start_offset_min:
    #  デフォルトは「締切をだいぶ過ぎた」時間帯に置くイメージ（ただし実時間には依存しない）
    default_offset = -(ARRIVAL_WINDOW_AFTER_MIN + NO_SHOW_GRACE_MIN + 5)
    base_offset = int(p.get("start_offset_min", default_offset))

    id_prefix = str(p.get("id_prefix", "sim_ns_"))

    created = []
    now_utc = datetime.now(timezone.utc)

    for i in range(num):
        rid = f"{id_prefix}{i+1}"

        # start/end は UTC で保存（既存のモック予約APIと同様の形式）
        start_utc = now_utc + timedelta(minutes=base_offset + i)
        end_utc   = start_utc + timedelta(minutes=duration_min)

        # 内部状態（_RSV_STATE）にも登録しておく
        _rsv_upsert(rid, start_utc, end_utc)

        # KPI: no_show_detected を即時に書き出す（no_show_watcher_loop と同じ形式 + simulated フラグ）
        _kpi_write({
            "ts": now_iso_jst(),
            "room_id": ROOM_ID,
            "bucket": AB_BUCKET,
            "event": "no_show_detected",
            "reservation_id": rid,
            "arrival_window_after_min": ARRIVAL_WINDOW_AFTER_MIN,
            "no_show_grace_min": NO_SHOW_GRACE_MIN,
            "simulated": True
        })

        # watcher が後で二重処理しないように auto_released を立てておく
        with _RS_LOCK:
            st = _RSV_STATE.get(rid)
            if st:
                st["auto_released"] = True

        created.append({
            "reservation_id": rid,
            "start_ts": start_utc.isoformat(),
            "end_ts": end_utc.isoformat()
        })

    return jsonify(
        ok=True,
        pattern=pattern,
        bucket=AB_BUCKET,
        num_reservations=num,
        reservations=created,
        note="no_show_detected のKPIイベントを擬似的に追加しただけで、在室人数やカメライベントは変更していません。"
    )

@app.post("/qr/checkin")
def qr_checkin():
    data = request.get_json(silent=True) or {}
    rid = data.get("reservation_id")
    ts = now_iso_jst()

    # ★ここを追加：厳格モードなら ID 無しで即失敗（APIは叩かない）
    if MOCK_QR_REQUIRE_ID and not rid:
        matched = False
    else:
        matched = bool(rid)
        if RESERVATION_API_BASE:
            try:
                r = requests.post(
                    f"{RESERVATION_API_BASE}/reservations/checkin-attempt",
                    json={"room_id": ROOM_ID, "ts": ts, "reservation_id": rid},
                    timeout=2
                )
                if r.ok:
                    jr = r.json()
                    matched = bool(jr.get("matched", matched))
                    rid = jr.get("reservation_id", rid)
            except Exception:
                pass

    # KPI更新（enterイベントとは独立に“チェックイン成功/失敗”を記録）
    if matched:
        # ★ここを修正：METRICS → _METRICS ＋ ロック
        with _O_LOCK:
            _METRICS["checkin_success"] += 1

        # これを追加（事前に mock_reservations_create で作ってあれば反映される）
        _record_checkin(rid)

        _kpi_write({
            "ts": ts, "room_id": ROOM_ID, "bucket": AB_BUCKET,
            "event": "qr_checkin", "matched": True,
            "reservation_id": rid, "method": "qr"
        })
        # 鍵許可（あれば）
        if LOCK_API_BASE:
            try:
                requests.post(f"{LOCK_API_BASE}/rooms/{ROOM_ID}/grant", timeout=2)
            except Exception:
                pass
        return jsonify(ok=True, matched=True, reservation_id=rid)
    else:
        with _O_LOCK:
            _METRICS["checkin_fail"] += 1

        _kpi_write({
            "ts": ts, "room_id": ROOM_ID, "bucket": AB_BUCKET,
            "event": "qr_checkin", "matched": False,
            "reservation_id": rid, "method": "qr"
        })
        return jsonify(ok=True, matched=False, reservation_id=rid), 404

@app.get("/metrics")
def metrics():
    with _O_LOCK:
        return jsonify({
            "occupancy": _OCCUPANCY,
            "counters": dict(_METRICS),
            "bucket": AB_BUCKET
        })

@app.get("/kpi/summary")
def kpi_summary():
    path = KPI_LOG_PATH
    if not path or not os.path.exists(path):
        return jsonify(ok=False, error="no_log"), 404
    enter = {}
    success = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                bucket = rec.get("bucket", "unknown")
                if rec.get("event") == "enter":
                    enter[bucket] = enter.get(bucket, 0) + 1
                    if rec.get("matched") is True:
                        success[bucket] = success.get(bucket, 0) + 1
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

    keys = sorted(set(enter.keys()) | set(success.keys()))
    out = []
    for b in keys:
        e = enter.get(b, 0)
        s = success.get(b, 0)
        rate = (s / e) if e else 0.0
        out.append({"bucket": b, "enter": e, "success": s, "rate": rate})
    return jsonify(ok=True, summary=out)

@app.get("/kpi/tail")
def kpi_tail():
    n = int(request.args.get("n", 200))
    p = KPI_LOG_PATH
    if not p or not os.path.exists(p):
        return jsonify(ok=False, error="no_log"), 404
    lines = []
    with open(p, "r", encoding="utf-8") as f:
        # ざっくり後ろから読む（ログが大きくなったら最適化）
        buf = f.readlines()[-n:]
    for line in buf:
        try:
            lines.append(json.loads(line))
        except:
            pass
    return jsonify(ok=True, items=lines, count=len(lines))

@app.route("/kpi/no_show_summary", methods=["GET"])
def kpi_no_show_summary():
    # JSONL から no_show_detected を集計
    total = 0
    buckets = {}
    try:
        with open(KPI_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("event") == "no_show_detected":
                    total += 1
                    b = row.get("bucket") or "unknown"
                    buckets[b] = buckets.get(b, 0) + 1
    except FileNotFoundError:
        pass
    return jsonify({"ok": True, "total_no_show": total, "by_bucket": buckets})

@app.get("/kpi/qr_summary")
def kpi_qr_summary():
    path = KPI_LOG_PATH
    if not path or not os.path.exists(path):
        return jsonify(ok=False, error="no_log"), 404
    per = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except:
                continue
            if rec.get("event") == "qr_checkin":
                b = rec.get("bucket","unknown")
                per.setdefault(b, {"qr_total":0,"qr_success":0})
                per[b]["qr_total"] += 1
                if rec.get("matched") is True:
                    per[b]["qr_success"] += 1
    out = [{"bucket": b,
            "qr_total": v["qr_total"],
            "qr_success": v["qr_success"],
            "qr_rate": (v["qr_success"]/v["qr_total"] if v["qr_total"] else 0.0)} for b,v in per.items()]
    return jsonify(ok=True, summary=out)

@app.post("/kpi/reset_log")
def kpi_reset_log():
    path = KPI_LOG_PATH
    if not path:
        return jsonify(ok=False, error="no_log_path"), 400
    try:
        # 空ファイルにする（なければ作る）
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            pass
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.post("/metrics/reset")
def metrics_reset():
    global _OCCUPANCY  # ★関数の先頭へ
    with _O_LOCK:
        _OCCUPANCY = 0
        for k in _METRICS.keys():
            _METRICS[k] = 0
    return jsonify(ok=True)

# ============= ローカルMock API（本配線前のE2E検証用） =============
@app.post("/mock/reservations/checkin-attempt")
def mock_rsv_checkin():
    """
    予約APIのモック:
      - reservation_id があれば、そのIDの有無/強制フラグで判定 (QR等のID指定用)
      - reservation_id がなければ、ts (時刻) を元に _RSV_STATE を検索して判定 (カメラ検知用)
    """
    payload = request.get_json(silent=True) or {}
    rid = payload.get("reservation_id")
    ts_str = payload.get("ts")
    force = request.args.get("matched")  # ?matched=true/false で強制上書き可

    # 1. 強制フラグがある場合
    if force is not None:
        matched = (force.lower() == "true")
        out_rid = rid or f"rsv_mock_forced_{int(time.time())}"
        return jsonify(matched=matched, reservation_id=out_rid, room_id=ROOM_ID)

    # 2. ID指定がある場合 (QRチェックイン等)
    if rid:
        # モックなので、単にIDがあればOKとする（本来はDB照合）
        matched = True
        return jsonify(matched=matched, reservation_id=rid, room_id=ROOM_ID)

    # 3. ID指定がない場合 (カメラ検知: 時間による照合)
    # _RSV_STATE から「現在時刻が含まれる予約」を探す
    matched = False
    found_rid = None
    
    try:
        # 判定基準時刻 (JST ISO文字列 -> datetime)
        if ts_str:
            check_dt = datetime.fromisoformat(ts_str)
        else:
            check_dt = datetime.now(timezone.utc) # 念のためUTC現在時刻

        # 到着許容ウィンドウ (configから取得。ここでは簡易的に前後15分などで検索)
        # 厳密には ARRIVAL_WINDOW_BEFORE_MIN 等を見るべきですが、モックなので「予約期間内」かチェック
        with _RS_LOCK:
            for r_id, st in _RSV_STATE.items():
                # 既にチェックイン済み/終了したものは除外するかはお好みで（ここでは何度でもHitする挙動にする）
                
                # 判定: 予約開始 <= 検知時刻 <= 予約終了 + 猶予
                # (本来は到着許容窓 T-10分 なども含める)
                window_start = st["start"] - timedelta(minutes=ARRIVAL_WINDOW_BEFORE_MIN)
                window_end   = st["end"]   + timedelta(minutes=NO_SHOW_GRACE_MIN)
                
                # check_dt が window 内にあれば Hit
                if window_start <= check_dt <= window_end:
                    matched = True
                    found_rid = r_id
                    break
    except Exception:
        pass

    if matched and found_rid:
        return jsonify(matched=True, reservation_id=found_rid, room_id=ROOM_ID)
    else:
        # Hitしなかった
        return jsonify(matched=False, reservation_id=None, room_id=ROOM_ID)

@app.post("/mock/rooms/<room_id>/grant")
def mock_lock_grant(room_id):
    """
    鍵APIのモック: 常に200 OK
    返すJSON: {"ok": true, "room_id": room_id}
    """
    return jsonify(ok=True, room_id=room_id)

# src/main.py の mock_reservations_create をこれに置き換え
@app.route("/mock/reservations/create", methods=["POST"])
def mock_reservations_create():
    """
    開発用：任意の予約を作成（重複チェック付き）
    """
    try:
        data = request.get_json(force=True) or {}
        rid = data.get("reservation_id") or f"rsv_{int(time.time())}"
        
        # 開始・終了時刻を計算
        start_in = int(data.get("start_in_min", 3))
        duration = int(data.get("duration_min", 30))
        
        new_start = datetime.now(timezone.utc) + timedelta(minutes=start_in)
        new_end   = new_start + timedelta(minutes=duration)

        # 重複チェック (Conflict Check)
        with _RS_LOCK:
            for existing_id, st in _RSV_STATE.items():
                if st.get("auto_released") or st.get("closed"):
                    continue
                
                # Overlap: (StartA < EndB) and (EndA > StartB)
                if (new_start < st["end"]) and (new_end > st["start"]):
                    return jsonify({
                        "ok": False, 
                        "error": "conflict", 
                        "message": f"予約重複: {existing_id} ({st['start'].strftime('%H:%M')}~)"
                    }), 409

            # 重複なければ登録
            _rsv_upsert(rid, new_start, new_end)

        return jsonify({
            "ok": True, 
            "reservation_id": rid, 
            "start_ts": new_start.isoformat(), 
            "end_ts": new_end.isoformat()
        })
    except Exception as e:
        # 万が一のエラー時に詳細を返す
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/mock/reservations/upcoming", methods=["GET"])
def mock_reservations_upcoming():
    """
    開発用：現在時刻から +60分の予約を返す
    query: ?room_id=R-0001
    """
    room_id = request.args.get("room_id") or ROOM_ID
    now = datetime.now(timezone.utc)
    until = now + timedelta(minutes=60)
    with _RS_LOCK:
        items = [
            {"reservation_id": rid,
             "room_id": room_id,
             "start_ts": st["start"].isoformat(),
             "end_ts": st["end"].isoformat(),
             "checked_in": st["checked_in"]}
            for rid, st in _RSV_STATE.items()
            if st["start"] <= until and st["end"] >= now
        ]
    return jsonify({"ok": True, "reservations": items})

@app.route("/mock/reservations/auto-release", methods=["POST"])
def mock_auto_release():
    """開発用：自動解放の受け口"""
    data = request.get_json(force=True) or {}
    rid = data.get("reservation_id")
    with _RS_LOCK:
        if rid in _RSV_STATE:
            _RSV_STATE[rid]["auto_released"] = True
    return jsonify({"ok": True, "reservation_id": rid, "action": "auto_released"})

@app.route("/reservations/extend", methods=["POST"])
def reservations_extend():
    """
    在室>0 なら予約の終了時刻を延長（モック）。
    body: {"reservation_id":"demo_001","extend_min":30}
    """
    data = request.get_json(force=True) or {}
    rid = data.get("reservation_id")
    add = int(data.get("extend_min", 15))
    if not rid:
        return jsonify(ok=False, error="reservation_id required"), 400

    with _O_LOCK:
        occ = _OCCUPANCY
    if occ <= 0:
        return jsonify(ok=False, error="no_occupancy"), 409

    with _RS_LOCK:
        st = _RSV_STATE.get(rid)
        if not st:
            return jsonify(ok=False, error="reservation_not_found"), 404
        st["end"] = st["end"] + timedelta(minutes=add)
        st["closed"] = False  # 延長したのでクローズ解除
        # オーバーステイフラグも念のため解除
        st["overstayed"] = False

    _kpi_write({
        "ts": now_iso_jst(),
        "room_id": ROOM_ID,
        "bucket": AB_BUCKET,
        "event": "reservation_extended",
        "reservation_id": rid,
        "extend_min": add
    })
    return jsonify(ok=True, reservation_id=rid, new_end_ts=st["end"].isoformat())

@app.route("/mock/locks/revoke", methods=["POST"])
def mock_locks_revoke():
    """開発用：鍵権限の取り消し"""
    return jsonify({"ok": True})

@app.get("/ui")
def ui_dashboard():
    lang = (request.args.get("lang") or "").lower()
    ja = (lang != "en")  # 既定は日本語

    if ja:
        html = r"""
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>匿名チェックイン ダッシュボード</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>.card{border:1px solid #e5e7eb;border-radius:1rem;padding:1rem}.grid{display:grid;gap:1rem}</style>
</head>
<body class="bg-gray-50 text-gray-900">
  <div class="max-w-6xl mx-auto p-6">
    <header class="mb-4">
      <div class="flex items-center justify-between">
        <h1 class="text-2xl font-bold">匿名チェックイン ダッシュボード</h1>
        <div class="text-sm text-gray-500" id="roomMeta"></div>
      </div>
      <!-- ★ 追加: 主要ページへのナビ -->
      <nav class="mt-2 text-sm flex flex-wrap gap-3">
        <a class="text-indigo-600 underline" href="/ui/qr">QRチェックイン（手入力/生成）</a>
        <a class="text-indigo-600 underline" href="/ui/scan">QRスキャン（カメラ読取）</a>
        <a class="text-indigo-600 underline" href="/ui/dev">開発者ツール</a>
        <a class="text-indigo-600 underline" href="/ui/signage" target="_blank" rel="noopener">掲示（プライバシー説明）</a>
        <span class="text-gray-300">|</span>
        <a class="text-gray-500 underline" href="/ui?lang=en">English</a>
      </nav>
    </header>

    <!-- ステータスカード -->
    <section class="grid md:grid-cols-4 grid-cols-2">
      <div class="card"><div class="text-sm text-gray-500">在室人数</div><div class="text-3xl font-semibold" id="occ">–</div></div>
      <div class="card"><div class="text-sm text-gray-500">A/Bバケット</div><div class="text-xl font-medium" id="bucket">–</div></div>
      <div class="card"><div class="text-sm text-gray-500">入室 / 退室</div><div class="text-xl" id="ee">– / –</div></div>
      <div class="card"><div class="text-sm text-gray-500">QR成功 / 失敗</div><div class="text-xl" id="qr">– / –</div></div>
    </section>

    <!-- KPIテーブル -->
    <section class="mt-6 grid md:grid-cols-3">
      <div class="card">
        <div class="font-semibold mb-2">A/B KPI（入室→予約ヒット）</div>
        <table class="w-full text-sm">
          <thead><tr class="text-gray-500"><th class="text-left">バケット</th><th class="text-right">入室</th><th class="text-right">ヒット</th><th class="text-right">率</th></tr></thead>
          <tbody id="kpiSummary"></tbody>
        </table>
      </div>
      <div class="card">
        <div class="font-semibold mb-2">QRチェックイン</div>
        <table class="w-full text-sm">
          <thead><tr class="text-gray-500"><th class="text-left">バケット</th><th class="text-right">総数</th><th class="text-right">成功</th><th class="text-right">率</th></tr></thead>
        <tbody id="qrSummary"></tbody>
        </table>
      </div>
      <div class="card">
        <div class="font-semibold mb-2">ノーショー（未到着）サマリ</div>
        <div class="text-sm">合計: <span id="noShowTotal">0</span></div>
        <table class="w-full text-sm mt-2">
          <thead><tr class="text-gray-500"><th class="text-left">バケット</th><th class="text-right">件数</th></tr></thead>
          <tbody id="noShowPer"></tbody>
        </table>
      </div>
    </section>

    <!-- デモ操作 -->
    <section class="mt-6 card">
      <div class="font-semibold mb-2">デモ操作</div>
      <div class="flex flex-wrap gap-2">
        <button class="px-3 py-2 rounded bg-indigo-600 text-white" onclick="post('/simulate/enter')">入室をシミュレート</button>
        <button class="px-3 py-2 rounded bg-indigo-600 text-white" onclick="post('/simulate/visit',{dwell_ms:500})">入退室（短時間滞在）</button>
        <button class="px-3 py-2 rounded bg-gray-700 text-white" onclick="post('/metrics/reset')">メトリクスをリセット</button>
      </div>
      <div class="mt-3 text-xs text-gray-500" id="opMsg"></div>
    </section>

    <footer class="mt-8 text-xs text-gray-400">
      ※ プライバシー最小化：映像は保存しません。匿名イベント（enter/exit, confidence）のみ処理・記録します。
      <span class="ml-2 text-gray-300">英語UIはヘッダの “English” リンクから</span>
    </footer>
  </div>

<script>
const fmtPct = x => (x===0? "0.0%" : (x? (x*100).toFixed(1)+"%" : "–"));
async function getJSON(url){ try{ const r=await fetch(url); if(!r.ok) throw new Error(r.status); return await r.json(); }catch{ return null; } }
async function post(url, body){
  try{
    const r = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body: body?JSON.stringify(body):'{}'});
    const j = await r.json().catch(()=>({}));
    const t = new Date().toLocaleTimeString('ja-JP',{hour12:false});
    document.getElementById('opMsg').textContent = `[${t}] ${url} -> ${r.status} ${JSON.stringify(j)}`;
  }catch{
    const t = new Date().toLocaleTimeString('ja-JP',{hour12:false});
    document.getElementById('opMsg').textContent = `[${t}] ${url} -> error`;
  }
}
async function refresh(){
  const m = await getJSON('/metrics');
  if(m){
    document.getElementById('occ').textContent = m.occupancy ?? '–';
    document.getElementById('bucket').textContent = m.bucket ?? '–';
    const c = m.counters || {};
    document.getElementById('ee').textContent = (c.enter??0) + ' / ' + (c.exit??0);
    document.getElementById('qr').textContent = (c.checkin_success??0) + ' / ' + (c.checkin_fail??0);
    document.getElementById('roomMeta').textContent = `A/B=${m.bucket??'–'}`;
  }
  const k = await getJSON('/kpi/summary');
  const tbody = document.getElementById('kpiSummary'); tbody.innerHTML='';
  if(k && k.ok && (k.summary||[]).length){
    k.summary.forEach(r=>{
      const tr=document.createElement('tr');
      tr.innerHTML = `<td>${r.bucket}</td>
                      <td class="text-right">${r.enter}</td>
                      <td class="text-right">${r.success}</td>
                      <td class="text-right">${fmtPct(r.rate)}</td>`;
      tbody.appendChild(tr);
    });
  }else{
    tbody.innerHTML = `<tr><td colspan="4" class="text-gray-400">データなし</td></tr>`;
  }
  const q = await getJSON('/kpi/qr_summary');
  const qtb = document.getElementById('qrSummary'); qtb.innerHTML='';
  if(q && q.ok && (q.summary||[]).length){
    q.summary.forEach(r=>{
      const tr=document.createElement('tr');
      tr.innerHTML = `<td>${r.bucket}</td>
                      <td class="text-right">${r.qr_total}</td>
                      <td class="text-right">${r.qr_success}</td>
                      <td class="text-right">${fmtPct(r.qr_rate)}</td>`;
      qtb.appendChild(tr);
    });
  }else{
    qtb.innerHTML = `<tr><td colspan="4" class="text-gray-400">データなし</td></tr>`;
  }
  const n = await getJSON('/kpi/no_show_summary');
  document.getElementById('noShowTotal').textContent = n?.total_no_show ?? 0;
  const ns=document.getElementById('noShowPer'); ns.innerHTML='';
  if(n && n.by_bucket){
    Object.entries(n.by_bucket).forEach(([b,cnt])=>{
      const tr=document.createElement('tr');
      tr.innerHTML = `<td>${b}</td><td class="text-right">${cnt}</td>`;
      ns.appendChild(tr);
    });
  }
}
refresh();
setInterval(refresh, 3000);  // 3秒ごとくらいで十分
</script>
</body>
</html>
        """
    else:
        # （英語版：必要なら /ui?lang=en で表示）
        html = r"""<!doctype html> ... (既存英語UIそのまま) ..."""
    return render_template_string(html)

@app.get("/ui/qr")
def ui_qr_manual():
    html = r"""
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>QRチェックイン（手入力/生成）</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
  <!-- 入力した文字列から“テスト用QR”を生成するための軽量ライブラリ -->
  <script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.3/build/qrcode.min.js"></script>
</head>
<body class="bg-gray-50 text-gray-900">
  <div class="max-w-3xl mx-auto p-6">
    <header class="mb-6">
      <h1 class="text-2xl font-bold">QRチェックイン（手入力/生成）</h1>
      <p class="text-sm text-gray-500 mt-1">部屋ID: {{room}} / A/B: {{bucket}}</p>
      <nav class="mt-2 text-sm">
        <a class="text-indigo-600 underline" href="/ui">← ダッシュボード</a> /
        <a class="text-indigo-600 underline" href="/ui/scan">カメラでQR読取へ</a> /
        <a class="text-indigo-600 underline" href="/ui/signage" target="_blank">掲示（プライバシー説明）</a>
      </nav>
    </header>

    <section class="bg-white border rounded-xl p-4">
      <div class="mb-3">
        <label class="block text-sm text-gray-600 mb-1">予約ID</label>
        <input id="rid" type="text" placeholder="例: demo_001"
               class="w-full border rounded-lg px-3 py-2 focus:outline-none focus:ring"
               autocomplete="off">
      </div>
      <div class="flex gap-2 flex-wrap">
        <button id="btnCheckin" class="px-4 py-2 rounded bg-indigo-600 text-white">チェックイン送信</button>
        <button id="btnMakeDemo" class="px-4 py-2 rounded bg-gray-700 text-white">デモ予約を作成</button>
        <button id="btnGenQR" class="px-4 py-2 rounded bg-emerald-600 text-white">入力値からQR生成</button>
      </div>
      <div id="msg" class="mt-3 text-sm"></div>
    </section>

    <section class="mt-6 grid md:grid-cols-2 gap-4">
      <div class="bg-white border rounded-xl p-4">
        <div class="font-semibold mb-2">テスト用QRプレビュー</div>
        <canvas id="qrCanvas" class="border rounded"></canvas>
        <p class="text-xs text-gray-500 mt-2">※ このQRは“予約IDの文字列”をそのままエンコードしただけです。</p>
      </div>
      <div class="bg-white border rounded-xl p-4">
        <div class="font-semibold mb-2">現在のメトリクス</div>
        <div class="text-sm grid grid-cols-2 gap-2">
          <div class="p-2 bg-gray-50 rounded">
            <div class="text-gray-500">在室人数</div>
            <div id="occ" class="text-xl font-semibold">–</div>
          </div>
          <div class="p-2 bg-gray-50 rounded">
            <div class="text-gray-500">QR成功 / 失敗</div>
            <div id="qr" class="text-xl">– / –</div>
          </div>
        </div>
      </div>
    </section>
  </div>

<script>
async function getJSON(u){try{const r=await fetch(u);return await r.json()}catch{return null}}
function jstNow(){return new Date().toLocaleString('ja-JP',{hour12:false})}
function showMsg(ok, text){
  const el=document.getElementById('msg');
  el.className='mt-3 text-sm '+(ok?'text-emerald-700':'text-rose-700');
  el.textContent = `[${jstNow()}] ${text}`;
}

async function refreshMetrics(){
  const m = await getJSON('/metrics');
  if(m){
    document.getElementById('occ').textContent = m.occupancy ?? '–';
    const c = m.counters||{};
    document.getElementById('qr').textContent = (c.checkin_success||0)+' / '+(c.checkin_fail||0);
  }
}

document.getElementById('btnCheckin').onclick = async ()=>{
  const rid = document.getElementById('rid').value.trim();
  try{
    const r = await fetch('/qr/checkin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reservation_id:rid})});
    const j = await r.json().catch(()=>({}));
    if(r.ok && j.matched){
      showMsg(true, `チェックイン成功: reservation_id=${j.reservation_id}`);
    }else{
      showMsg(false, `チェックイン失敗: ${JSON.stringify(j)}`);
    }
  }catch(e){
    showMsg(false, `通信エラー: ${e}`);
  }
  refreshMetrics();
};

document.getElementById('btnMakeDemo').onclick = async ()=>{
  try{
    const r = await fetch('/mock/reservations/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reservation_id:'demo_001',start_in_min:3,duration_min:30})});
    const j = await r.json();
    document.getElementById('rid').value = j.reservation_id || 'demo_001';
    showMsg(true, `デモ予約を作成しました（開始: ${j.start_ts} / 終了: ${j.end_ts}）`);
  }catch(e){
    showMsg(false, `作成失敗: ${e}`);
  }
};

document.getElementById('btnGenQR').onclick = ()=>{
  const rid = document.getElementById('rid').value.trim();
  const canvas = document.getElementById('qrCanvas');
  if(!rid){ showMsg(false,'先に「予約ID」を入力してください'); return; }
  QRCode.toCanvas(canvas, rid, {width:256,margin:1}, err=>{
    if(err){ showMsg(false, 'QR生成に失敗: '+err); return;}
    showMsg(true, 'QRを生成しました。/ui/scan で読み取りできます。');
  });
};

refreshMetrics();
setInterval(refreshMetrics, 3000);
</script>
</body>
</html>
    """
    return render_template_string(html, room=ROOM_ID, bucket=AB_BUCKET)

@app.get("/ui/scan")
def ui_qr_scan():
    html = r"""
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>QRスキャンでチェックイン</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
  <!-- カメラからQRを読み取るライブラリ（ブラウザだけで完結） -->
  <script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script>
</head>
<body class="bg-gray-50 text-gray-900">
  <div class="max-w-xl mx-auto p-6">
    <header class="mb-4">
      <h1 class="text-2xl font-bold">QRスキャンでチェックイン</h1>
      <p class="text-sm text-gray-500 mt-1">部屋ID: {{room}} / A/B: {{bucket}}</p>
      <nav class="mt-2 text-sm">
        <a class="text-indigo-600 underline" href="/ui">← ダッシュボード</a> /
        <a class="text-indigo-600 underline" href="/ui/qr">手入力へ</a>
      </nav>
    </header>

    <section class="bg-white border rounded-xl p-4">
      <div class="mb-2 text-sm text-gray-600">
        予約アプリに表示されるQRをカメラにかざしてください。
      </div>
      <div id="reader" class="w-full"></div>
      <div id="msg" class="mt-3 text-sm"></div>
      <div class="mt-3">
        <button id="btnRestart" class="px-3 py-2 rounded bg-gray-700 text-white hidden">再スキャン</button>
      </div>
    </section>
  </div>

<script>
function jstNow(){return new Date().toLocaleString('ja-JP',{hour12:false})}
function show(ok, text){
  const el=document.getElementById('msg');
  el.className='mt-3 text-sm '+(ok?'text-emerald-700':'text-rose-700');
  el.textContent = `[${jstNow()}] ${text}`;
}

let scanner=null;
async function start(){
  try{
    scanner = new Html5Qrcode("reader");
    const config = {fps:10, qrbox:{width:250,height:250}};
    await scanner.start(
      { facingMode: "environment" },
      config,
      async (decodedText, decodedResult) => {
        // 1回読めたら止める
        try{ await scanner.stop(); }catch{}
        document.getElementById('btnRestart').classList.remove('hidden');

        const rid = decodedText.trim();
        show(true, `QR読み取り成功: ${rid} → チェックイン送信中…`);

        try{
          const r = await fetch('/qr/checkin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reservation_id:rid})});
          const j = await r.json().catch(()=>({}));
          if(r.ok && j.matched){
            show(true, `チェックイン成功: reservation_id=${j.reservation_id}`);
          }else{
            show(false, `チェックイン失敗: ${JSON.stringify(j)}`);
          }
        }catch(e){
          show(false, `通信エラー: ${e}`);
        }
      },
      (errorMsg) => {
        // 連続で来るのでログは抑制（必要なら console.log）
      }
    );
  }catch(e){
    show(false, `カメラ起動に失敗しました。権限やHTTPSをご確認ください。(${e}) / 手入力ページ: /ui/qr`);
  }
}

document.getElementById('btnRestart').onclick = async ()=>{
  document.getElementById('btnRestart').classList.add('hidden');
  document.getElementById('msg').textContent='';
  // 既存DOMをクリアしてから再起動
  document.getElementById('reader').innerHTML='';
  await start();
};

start();
</script>
</body>
</html>
    """
    return render_template_string(html, room=ROOM_ID, bucket=AB_BUCKET)

@app.get("/ui/signage")
def ui_signage():
    html = r"""
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>来訪検知システムのご案内</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-white text-gray-900">
  <div class="max-w-3xl mx-auto p-8">
    <h1 class="text-2xl font-bold mb-4">来訪検知システムのご案内</h1>
    <ul class="list-disc pl-6 space-y-2">
      <li>目的：本スペースの「来訪有無」の判定と運営（予約チェックイン、自動解放）</li>
      <li>プライバシー最小化：映像や音声の保存は行いません。入退室の匿名イベントのみを処理します。</li>
      <li>個人識別なし：顔認識・特徴量の生成は行いません。</li>
      <li>設置：入口ラインのみを検知対象としています（更衣・私有領域は撮影しません）。</li>
      <li>お問い合わせ先：運営事務局（{{room}}）</li>
    </ul>
    <p class="mt-6 text-sm text-gray-500">※自動チェックインが難しい場合は受付のQR端末をご利用ください。</p>
  </div>
</body>
</html>
    """
    return render_template_string(html, room=ROOM_ID)

@app.get("/ui/dev")
def ui_dev():
    html = r"""
<!doctype html>
<html lang="ja"><head>
  <meta charset="utf-8"><title>開発者ツール (Full)</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    .card{border:1px solid #e5e7eb;border-radius:0.75rem;padding:1rem;background:white}
    .timeline-container { position: relative; height: 60px; background: #f3f4f6; border-radius: 8px; overflow: hidden; margin-top: 10px; border:1px solid #ddd; }
    .timeline-bar { position: absolute; top: 10px; height: 40px; background: #34d399; border-radius: 4px; opacity: 0.8; text-align: center; line-height: 40px; color: #064e3b; font-size: 10px; white-space: nowrap; overflow: hidden; border:1px solid #10b981; transition: all 0.3s;}
    .timeline-now { position: absolute; top: 0; bottom: 0; width: 2px; background: #e11d48; z-index: 10; box-shadow: 0 0 4px rgba(225,29,72,0.5); }
    .timeline-checkin { background: #60a5fa !important; border-color: #2563eb !important; color: #1e3a8a !important; }
    .timeline-noshow { background: #fb7185 !important; border-color: #e11d48 !important; color: #881337 !important; }
  </style>
</head>
<body class="bg-gray-50 text-gray-900">
<div class="max-w-7xl mx-auto p-4 space-y-6">
  <header class="flex items-center justify-between">
    <h1 class="text-2xl font-bold">開発者ツール <span class="text-sm font-normal text-gray-500 ml-2">with Timeline</span></h1>
    <nav class="text-sm"><a class="text-indigo-600 underline" href="/ui">← ダッシュボードへ</a></nav>
  </header>

  <section class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="card"><div class="text-sm text-gray-500">在室人数</div><div class="text-3xl font-semibold" id="occ">–</div></div>
    <div class="card"><div class="text-sm text-gray-500">A/Bバケット</div><div class="text-xl font-medium" id="bucket">–</div></div>
    <div class="card"><div class="text-sm text-gray-500">入室 / 退室</div><div class="text-xl" id="ee">– / –</div></div>
    <div class="card"><div class="text-sm text-gray-500">QR成功 / 失敗</div><div class="text-xl" id="qr">– / –</div></div>
  </section>

  <hr class="border-gray-300">

  <section class="bg-white border rounded-xl p-4 shadow-sm">
    <div class="flex justify-between items-center mb-2">
      <h2 class="font-bold text-lg">部屋予約状況 (Timeline)</h2>
      <div class="text-xs text-gray-500">現在時刻: <span id="clock" class="font-mono">--:--:--</span></div>
    </div>
    
    <div class="relative">
        <div class="flex justify-between text-xs text-gray-400 mb-1"><span>Now - 30min</span><span>Now</span><span>Now + 60min</span></div>
        <div id="timeline" class="timeline-container">
            <div class="timeline-now" style="left: 33.3%;"></div>
        </div>
    </div>

    <details class="mt-2">
      <summary class="text-xs text-indigo-600 cursor-pointer">予約リスト(Raw JSON)を表示</summary>
      <pre id="rsv" class="text-xs bg-gray-50 rounded p-2 mt-1 overflow-auto h-24 border"></pre>
    </details>

    <div class="mt-4 bg-gray-50 p-3 rounded border">
      <div class="font-semibold text-sm mb-2">予約操作</div>
      <div class="flex flex-wrap gap-2 items-end text-sm mb-2">
        <label class="flex flex-col"><span>ID</span><input id="newRid" type="text" value="demo_001" class="border rounded px-2 py-1 w-24"></label>
        <label class="flex flex-col"><span>開始(分後)</span><input id="newStart" type="number" value="5" class="border rounded px-2 py-1 w-16"></label>
        <label class="flex flex-col"><span>長さ(分)</span><input id="newDur" type="number" value="30" class="border rounded px-2 py-1 w-16"></label>
        <button class="px-4 py-1 bg-indigo-600 text-white rounded shadow" onclick="createCustomRsv()">予約作成</button>
        <button class="px-4 py-1 bg-gray-200 text-gray-700 rounded shadow" onclick="clearReservations()">クリア</button>
      </div>
      <div id="rsvMsg" class="text-xs p-2 rounded font-bold hidden"></div>

      <div class="mt-3 pt-2 border-t flex items-end gap-2">
        <span class="text-xs font-bold text-gray-600">延長操作:</span>
        <label class="flex flex-col text-xs"><span>Target ID</span><input id="opRid" value="demo_001" class="border rounded px-2 py-1 w-24"></label>
        <label class="flex flex-col text-xs"><span>Extend(min)</span><input id="opExt" type="number" value="15" class="border rounded px-2 py-1 w-16"></label>
        <button class="px-3 py-1 bg-emerald-600 text-white rounded text-xs" onclick="opExtend()">延長</button>
      </div>
      <div id="opOut" class="text-xs text-gray-500 mt-1"></div>
    </div>
  </section>

  <section class="grid md:grid-cols-2 gap-4">
    <div class="bg-white border rounded-xl p-4">
      <div class="font-semibold mb-2 text-sm">カメラ検知シミュレーション</div>
      <div class="flex gap-2 mb-2">
        <button class="flex-1 px-3 py-2 bg-indigo-600 text-white rounded text-sm shadow" onclick="runSingle()">1人来訪 (Visit)</button>
        <button class="flex-1 px-3 py-2 bg-emerald-600 text-white rounded text-sm shadow" onclick="runMulti()">5人ランダム</button>
      </div>
      <div class="flex gap-2 text-xs mb-2">
         <button class="flex-1 py-1 bg-gray-600 text-white rounded" onclick="runMultiForBucket('treatment')">treatmentで実行</button>
         <button class="flex-1 py-1 bg-gray-600 text-white rounded" onclick="runMultiForBucket('control')">controlで実行</button>
      </div>
      <pre id="scenarioOut" class="text-xs bg-gray-50 rounded p-2 h-20 overflow-auto mt-2 border"></pre>
    </div>

    <div class="space-y-4">
       <div class="bg-white border rounded-xl p-4">
          <div class="font-semibold mb-2 text-sm">予約シナリオ (ノーショー)</div>
          <div class="flex items-end gap-2">
            <label class="text-xs">件数 <input id="rsvNum" type="number" value="3" class="border rounded px-1 w-12"></label>
            <label class="text-xs">Prefix <input id="rsvPrefix" type="text" value="sim_ns_" class="border rounded px-1 w-20"></label>
            <button class="px-3 py-2 bg-rose-600 text-white rounded text-sm flex-1 shadow" onclick="runNoShowScenario()">ノーショー生成</button>
          </div>
          <div id="rsvScenarioOut" class="mt-1 text-xs text-gray-500 truncate"></div>
       </div>
       <div class="bg-white border rounded-xl p-4">
          <div class="font-semibold mb-1 text-sm">KPIサマリ</div>
          <div id="miniKpi" class="text-xs text-gray-600 h-20 overflow-auto">Loading...</div>
       </div>
    </div>
  </section>

  <section class="bg-white border rounded-xl p-4">
      <div class="font-semibold mb-2 text-sm">管理・設定</div>
      
      <div class="grid grid-cols-3 md:grid-cols-6 gap-2 text-xs mb-3">
         <label>Axis <input id="line_axis" class="border w-full" value="y"></label>
         <label>Pos <input id="line_pos" class="border w-full" value="360"></label>
         <label>Score <input id="min_score" class="border w-full" value="0.6"></label>
         <label>Conf <input id="conf_min" class="border w-full" value="0.9"></label>
         <label>WH <input id="min_wh" class="border w-full" value="20"></label>
         <label>IoU <input id="iou_match" class="border w-full" value="0.3"></label>
      </div>
      <div class="flex gap-2 mb-3">
           <button class="px-3 py-1 bg-gray-500 text-white rounded text-xs" onclick="updateCfg()">Update Config</button>
           <div class="flex items-center gap-1 text-xs ml-2">
             Window:
             <input id="arr_b" class="border w-8" value="10">
             <input id="arr_a" class="border w-8" value="15">
             <input id="grace" class="border w-8" value="7">
             <button class="bg-blue-600 text-white px-2 rounded" onclick="updateWindow()">Set</button>
           </div>
      </div>

      <div class="flex flex-wrap gap-2 pt-2 border-t">
        <button class="px-3 py-1 bg-gray-200 rounded text-xs" onclick="setBucket('treatment')">Bucket: Treatment</button>
        <button class="px-3 py-1 bg-gray-200 rounded text-xs" onclick="setBucket('control')">Bucket: Control</button>
        <button class="px-3 py-1 bg-gray-700 text-white rounded text-xs" onclick="resetMetrics()">カウンタReset</button>
        <button class="px-3 py-1 bg-rose-600 text-white rounded text-xs" onclick="resetKpiLog()">KPIログReset</button>
      </div>
      <div id="adminMsg" class="text-xs text-gray-500 mt-1"></div>
  </section>
  
  <section class="bg-white border rounded-xl p-4">
    <div class="flex justify-between items-center mb-1">
      <div class="font-semibold text-sm">KPIログ末尾</div>
      <button class="text-xs bg-gray-200 px-2 py-1 rounded" onclick="loadTail()">更新</button>
    </div>
    <pre id="tail" class="text-xs bg-gray-50 rounded p-3 overflow-auto h-32 border"></pre>
  </section>
</div>

<script>
// --- 通信ユーティリティ ---
async function j(u,m='GET',b){
  const o={method:m,headers:{'Content-Type':'application/json'}};
  if(b)o.body=JSON.stringify(b);
  const r = await fetch(u,o);
  if(!r.ok) {
    let errText = r.statusText;
    try { const ej = await r.json(); if(ej.message) errText = ej.message; else if(ej.error) errText = ej.error; } catch{}
    throw new Error(`[${r.status}] ${errText}`);
  }
  try{ return await r.json() } catch(e){ return {} }
}
function setText(id,t){const el=document.getElementById(id);if(el)el.textContent=(typeof t==='string'?t:JSON.stringify(t,null,2));}
const fmtPct = x => (x===0? "0.0%" : (x? (x*100).toFixed(1)+"%" : "–"));

// --- Timeline Logic ---
let cachedReservations = [];
async function loadUpcoming(){
  try {
    // 予約リストを取得して、Raw表示とタイムラインの両方を更新
    const r=await j('/mock/reservations/upcoming');
    if(r.reservations) {
      cachedReservations = r.reservations;
      setText('rsv', cachedReservations); // Rawリスト更新
    }
    renderTimeline();
  } catch(e) { console.error(e); }
}

function renderTimeline(){
  const el = document.getElementById('timeline');
  // 赤線(Now)だけ残してクリアせず、毎回全描画(単純化)
  el.innerHTML = '<div class="timeline-now" style="left: 33.3%;"></div>';
  
  const now = new Date();
  const startWin = new Date(now.getTime() - 30*60000); // 30分前
  const totalMs = 90 * 60000; // 全体90分幅

  cachedReservations.forEach(r => {
    const start = new Date(r.start_ts);
    const end = new Date(r.end_ts);
    
    const leftPct = ((start - startWin) / totalMs) * 100;
    const widthPct = ((end - start) / totalMs) * 100;

    // 画面外なら描画しない
    if(leftPct + widthPct < 0 || leftPct > 100) return;

    const bar = document.createElement('div');
    bar.className = 'timeline-bar';
    if(r.checked_in) bar.classList.add('timeline-checkin');
    if(r.auto_released) bar.classList.add('timeline-noshow');
    
    bar.style.left = Math.max(0, leftPct) + '%';
    bar.style.width = Math.min(100 - Math.max(0, leftPct), widthPct) + '%';
    bar.innerText = r.reservation_id;
    el.appendChild(bar);
  });
  
  document.getElementById('clock').innerText = now.toLocaleTimeString();
}

// --- Actions ---
async function createCustomRsv(){
  const rid = document.getElementById('newRid').value;
  const start = Number(document.getElementById('newStart').value);
  const dur = Number(document.getElementById('newDur').value);
  const msg = document.getElementById('rsvMsg');
  
  msg.className = "text-xs p-2 rounded font-bold hidden";
  msg.textContent = "";

  try {
    const res = await j('/mock/reservations/create','POST',{reservation_id:rid, start_in_min:start, duration_min:dur});
    msg.textContent = "作成成功: " + (res.reservation_id || rid);
    msg.className = "text-xs p-2 rounded font-bold bg-emerald-100 text-emerald-700 block";
    
    // 即時更新
    await loadUpcoming();
    
    // IDインクリメント
    const num = parseInt(rid.replace(/\D/g,'')) || 0;
    document.getElementById('newRid').value = rid.replace(/\d+/, '') + (num+1);
  } catch(e) {
    msg.textContent = "作成失敗: " + e.message;
    msg.className = "text-xs p-2 rounded font-bold bg-rose-100 text-rose-700 block";
  }
}

async function clearReservations(){ alert("リロードします"); location.reload(); }
async function opExtend(){
  const rid = document.getElementById('opRid').value.trim();
  const ext = Number(document.getElementById('opExt').value)||15;
  try{
     const r = await j('/reservations/extend','POST',{reservation_id:rid, extend_min:ext});
     setText('opOut', "延長成功: "+r.new_end_ts);
     loadUpcoming();
  }catch(e){ setText('opOut', "失敗: "+e.message); }
}

async function setBucket(b){ const r=await j('/ab/set','POST',{bucket:b}); setText('adminMsg', `Bucket: ${r.bucket}`); refreshMonitor(); }
async function updateCfg(){
  const p={
    line_axis:document.getElementById('line_axis').value,
    line_pos:Number(document.getElementById('line_pos').value),
    min_score:Number(document.getElementById('min_score').value),
    min_wh:Number(document.getElementById('min_wh').value),
    iou_match:Number(document.getElementById('iou_match').value),
    confidence_min:Number(document.getElementById('conf_min').value)
  };
  await j('/config/update','POST',p);
  setText('adminMsg', 'Config Updated');
}
async function updateWindow(){
  await j('/config/update','POST',{
    arrival_before_min:Number(document.getElementById('arr_b').value),
    arrival_after_min:Number(document.getElementById('arr_a').value),
    no_show_grace_min:Number(document.getElementById('grace').value)
  });
  setText('adminMsg', 'Window Updated');
}

async function resetMetrics(){ await j('/metrics/reset','POST',{}); refreshMonitor(); }
async function resetKpiLog(){ await j('/kpi/reset_log','POST',{}); refreshMonitor(); }

async function runSingle(){ try{ const r = await j('/simulate/visit', 'POST', {dwell_ms:800}); setText('scenarioOut', r); refreshMonitor(); }catch(e){setText('scenarioOut',e.message)} }
async function runMulti(){ try{ const r = await j('/simulate/scenario', 'POST', {visitors:5}); setText('scenarioOut', r); refreshMonitor(); }catch(e){setText('scenarioOut',e.message)} }
async function runMultiForBucket(bucket){ await j('/ab/set', 'POST', {bucket: bucket}); runMulti(); }
async function runNoShowScenario(){
  const num = Number(document.getElementById('rsvNum').value) || 3;
  const prefix = document.getElementById('rsvPrefix').value || 'sim_ns_';
  const r = await j('/simulate/reservation_scenario', 'POST', {pattern:'no_show',num_reservations:num,id_prefix:prefix});
  setText('rsvScenarioOut', `Done: ${num} records`);
  refreshMonitor();
}

// --- Monitor Loop ---
async function refreshMonitor(){
  // 1. Metrics
  const m = await j('/metrics');
  if(m && m.counters){
    setText('occ', m.occupancy ?? '–');
    setText('bucket', m.bucket ?? '–');
    setText('ee', (m.counters.enter??0) + ' / ' + (m.counters.exit??0));
    setText('qr', (m.counters.checkin_success??0) + ' / ' + (m.counters.checkin_fail??0));
  }
  
  // 2. KPI Summary (Mini)
  const k = await j('/kpi/summary');
  const mk = document.getElementById('miniKpi');
  if(mk){
    let h = '<table class="w-full"><tr><td>Bucket</td><td align="right">Ent</td><td align="right">Hit</td></tr>';
    (k.summary||[]).forEach(r=>{ h+=`<tr><td>${r.bucket}</td><td align="right">${r.enter}</td><td align="right">${r.success} (${fmtPct(r.rate)})</td></tr>`; });
    h+='</table>';
    mk.innerHTML = h;
  }
  
  // 3. 予約情報の同期 (タイムライン更新)
  // ★ここが重要: ループごとに最新情報を取得して描画する
  await loadUpcoming();
}

// Init
loadUpcoming();
setInterval(refreshMonitor, 1000); // 1秒ごとに更新
</script>
</body></html>
    """
    return render_template_string(html)

# ============= 起動時にポーラ開始 =============
def no_show_watcher_loop():
    """
    30秒おきに未チェックインの予約を走査し、
    (start + ARRIVAL_WINDOW_AFTER_MIN + NO_SHOW_GRACE_MIN) を超過したら no-show と判定して解放。
    """
    interval_sec = 30
    while True:
        try:
            now = datetime.now(timezone.utc)
            with _RS_LOCK:
                items = list(_RSV_STATE.items())
            for rid, st in items:
                if st["checked_in"] or st["auto_released"]:
                    continue
                window_end = st["start"] + timedelta(minutes=ARRIVAL_WINDOW_AFTER_MIN)
                deadline   = window_end + timedelta(minutes=NO_SHOW_GRACE_MIN)
                if now >= deadline:
                    # no-show 検知
                    with _RS_LOCK:
                        st["auto_released"] = True

                    # KPI ログ
                    _kpi_write({
                        "ts": now_iso_jst(),   # ★ここを now.isoformat() からJSTに
                        "room_id": ROOM_ID,
                        "bucket": AB_BUCKET,
                        "event": "no_show_detected",
                        "reservation_id": rid,
                        "arrival_window_after_min": ARRIVAL_WINDOW_AFTER_MIN,
                        "no_show_grace_min": NO_SHOW_GRACE_MIN
                    })

                    # 予約APIに自動解放通知（モック/本番どちらでもOK）
                    if RESERVATION_API_BASE:
                        try:
                            requests.post(f"{RESERVATION_API_BASE}/reservations/auto-release",
                                          json={"reservation_id": rid, "room_id": ROOM_ID}, timeout=2)
                        except Exception:
                            pass

                    # 鍵APIの権限取り消し（あれば）
                    if LOCK_API_BASE:
                        try:
                            requests.post(f"{LOCK_API_BASE}/locks/revoke",
                                          json={"reservation_id": rid, "room_id": ROOM_ID}, timeout=2)
                        except Exception:
                            pass

        except Exception as e:
            app.logger.exception("no_show_watcher error: %s", e)
        time.sleep(interval_sec)

def end_watcher_loop():
    """
    30秒おきに各予約をスキャンし、終了前後のウィンドウでクローズ判定、
    終了＋猶予超過の在室>0をオーバーステイとして記録。
    """
    interval_sec = 30
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            with _RS_LOCK:
                items = list(_RSV_STATE.items())

            for rid, st in items:
                if st["auto_released"]:
                    continue

                # 自動クローズ：終了±END_CLOSE_WINDOW_MIN の窓内で在室=0なら閉じる
                close_from = st["end"] - timedelta(minutes=END_CLOSE_WINDOW_MIN)
                close_to   = st["end"] + timedelta(minutes=END_CLOSE_WINDOW_MIN)
                if (not st.get("closed")) and (close_from <= now_utc <= close_to):
                    with _O_LOCK:
                        occ = _OCCUPANCY
                    if occ == 0:
                        st["closed"] = True
                        _kpi_write({
                            "ts": now_iso_jst(),
                            "room_id": ROOM_ID,
                            "bucket": AB_BUCKET,
                            "event": "reservation_closed",
                            "reservation_id": rid,
                            "reason": "zero_occupancy_near_end",
                            "end_close_window_min": END_CLOSE_WINDOW_MIN
                        })
                        # 鍵のクローズ/権限リボーク（モック/本番）
                        if LOCK_API_BASE:
                            try:
                                requests.post(f"{LOCK_API_BASE}/locks/revoke",
                                              json={"reservation_id": rid, "room_id": ROOM_ID}, timeout=2)
                            except Exception:
                                pass

                # オーバーステイ検出：終了 + OVERSTAY_GRACE_MIN 超え、かつ在室>0
                deadline = st["end"] + timedelta(minutes=OVERSTAY_GRACE_MIN)
                if (not st.get("overstayed")) and (now_utc >= deadline):
                    with _O_LOCK:
                        occ = _OCCUPANCY
                    if occ > 0:
                        st["overstayed"] = True
                        _kpi_write({
                            "ts": now_iso_jst(),
                            "room_id": ROOM_ID,
                            "bucket": AB_BUCKET,
                            "event": "overstay_detected",
                            "reservation_id": rid,
                            "overstay_grace_min": OVERSTAY_GRACE_MIN,
                            "occupancy": occ
                        })
                        # 予約API通知（課金/警告のフック）
                        if RESERVATION_API_BASE:
                            try:
                                requests.post(f"{RESERVATION_API_BASE}/reservations/overstay",
                                              json={"reservation_id": rid, "room_id": ROOM_ID,
                                                    "occupancy": occ}, timeout=2)
                            except Exception:
                                pass
        except Exception as e:
            app.logger.exception("end_watcher error: %s", e)

        time.sleep(interval_sec)

def _public_urls(host: str, port: int, path: str) -> list[str]:
    """
    Codespaces の URL 形式が環境で揺れるため候補を複数返す。
    例:
      - https://{name}-{port}.app.github.dev (←今回ユーザー環境で正)
      - https://{port}-{name}.app.github.dev (公式ドキュメント表記)
    Gitpod も念のため両方返す。
    最後にローカル URL をフォールバックとして含める。
    """
    urls = []

    # 明示オーバーライド（.env で PUBLIC_URL_BASE を与えたら最優先）
    override = os.getenv("PUBLIC_URL_BASE")
    if override:
        urls.append(override.rstrip("/") + path)

    # GitHub Codespaces
    if os.environ.get("CODESPACES") == "true":
        name = os.environ.get("CODESPACE_NAME")
        domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
        if name:
            # ユーザー環境で有効だった形式（name-port）を先頭に
            urls.append(f"https://{name}-{port}.{domain}{path}")
            # 逆順（port-name）も候補に
            urls.append(f"https://{port}-{name}.{domain}{path}")

    # Gitpod（ついでに両順序）
    gp = os.environ.get("GITPOD_WORKSPACE_URL")
    if gp:
        base = gp.rstrip("/").replace("https://", "")
        urls.append(f"https://{base.split('/')[0].split('.')[0]}-{port}.{'.'.join(base.split('.')[1:])}{path}")
        urls.append(gp.replace("https://", f"https://{port}-") + path)

    # 最後はローカル
    urls.append(f"http://{host}:{port}{path}")
    # 重複削除（順序維持）
    seen = set()
    uniq = []
    for u in urls:
        if u not in seen:
            uniq.append(u)
            seen.add(u)
    return uniq

def _auto_open_ui(host="127.0.0.1", port=8000, path="/ui"):
    # AUTO_OPEN_UI=0 で無効化
    if os.environ.get("AUTO_OPEN_UI", "1") != "1":
        return

    candidates = _public_urls(host, port, path)

    def _try_open(urls: list[str]):
        # 到達チェック（外から見える URL を優先）。失敗してもログだけ。
        chosen = None
        for u in urls:
            try:
                # サーバ起動直後なので少し待つ
                time.sleep(0.3)
                r = requests.get(u, timeout=2, allow_redirects=True)
                if r.status_code != 404:
                    chosen = u
                    break
            except Exception:
                continue
        if chosen is None:
            # チェックに失敗したら先頭候補で開く
            chosen = urls[0]
        try:
            ok = webbrowser.open(chosen, new=2)
            print(f"[ui] open_attempt ok={ok} url={chosen}", flush=True)
        except Exception as e:
            print(f"[ui] auto open failed: {e} (URL: {chosen})", flush=True)

    # 見えるログ（クリック用に全候補を出す）
    print("[ui] candidates:", *candidates, sep="\n  - ", flush=True)

    # 少し遅らせて実行（reloader子プロセスでだけ実行される想定）
    threading.Timer(0.8, _try_open, args=(candidates,)).start()

def _start_threads():
    # 監視は常時
    threading.Thread(target=no_show_watcher_loop, daemon=True, name="no_show_watcher").start()
    threading.Thread(target=end_watcher_loop,    daemon=True, name="end_watcher").start()
    # エッジポーラは環境変数で無効化可能
    if not DISABLE_POLLER:
        threading.Thread(target=edge_poller_loop, daemon=True, name="edge_poller").start()

if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _start_threads()
        _auto_open_ui("127.0.0.1", 8000, "/ui")
    app.run(host="0.0.0.0", port=8000, debug=True)