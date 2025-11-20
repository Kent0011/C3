from flask import Flask, jsonify, request, render_template_string, redirect
import Repository
import os, json, time, threading
import webbrowser
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import traceback
import requests
import random

# ============= 基本セットアップ =============
load_dotenv()
app = Flask(__name__)

ai = Repository.AiCameraRepository(
    console_endpoint=os.getenv("CONSOLE_ENDPOINT"),
    auth_endpoint=os.getenv("AUTH_ENDPOINT"),
    client_id=os.getenv("CLIENT_ID"),
    client_secret=os.getenv("CLIENT_SECRET"),
    device_id=os.getenv("DEVICE_ID"),
)

os.environ.setdefault("TZ", "Asia/Tokyo")
try:
    time.tzset()
except Exception:
    pass

JST = timezone(timedelta(hours=9))
def now_iso_jst() -> str:
    return datetime.now(JST).isoformat()

# ============= 運用パラメータ =============
ROOM_ID = os.getenv("ROOM_ID", "R-0001")
POLL_FPS = float(os.getenv("POLL_FPS", "2.0"))
LINE_AXIS = os.getenv("LINE_AXIS", "y")
LINE_POS = int(os.getenv("LINE_POS", "360"))
DIR_POSITIVE_IS_ENTER = (os.getenv("DIR_POSITIVE_IS_ENTER", "1") == "1")
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.6"))
MIN_WH = int(os.getenv("MIN_WH", "20"))
IOU_MATCH = float(os.getenv("IOU_MATCH", "0.3"))
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.9"))
MOCK_RESERVATION_MATCH = (os.getenv("MOCK_RESERVATION_MATCH", "1") == "1")
RESERVATION_API_BASE = os.getenv("RESERVATION_API_BASE")
RESERVATION_API_KEY  = os.getenv("RESERVATION_API_KEY")
LOCK_API_BASE        = os.getenv("LOCK_API_BASE")
LOCK_API_KEY         = os.getenv("LOCK_API_KEY")

LC = None
def _make_line_counter():
    return LineCounter(
        axis=LINE_AXIS, pos=LINE_POS,
        dir_positive_is_enter=DIR_POSITIVE_IS_ENTER,
        iou_match=IOU_MATCH, min_score=MIN_SCORE, min_wh=MIN_WH
    )
DISABLE_POLLER = (os.getenv("DISABLE_POLLER","0") == "1")

AB_BUCKET = os.getenv("AB_BUCKET", "treatment")
KPI_LOG_PATH = os.getenv("KPI_LOG_PATH")

ARRIVAL_WINDOW_BEFORE_MIN = int(os.getenv("ARRIVAL_WINDOW_BEFORE_MIN", "10"))
ARRIVAL_WINDOW_AFTER_MIN  = int(os.getenv("ARRIVAL_WINDOW_AFTER_MIN", "15"))
NO_SHOW_GRACE_MIN         = int(os.getenv("NO_SHOW_GRACE_MIN", "7"))
MOCK_QR_REQUIRE_ID = (os.getenv("MOCK_QR_REQUIRE_ID", "0") == "1")

END_CLOSE_WINDOW_MIN = int(os.getenv("END_CLOSE_WINDOW_MIN", "5"))
OVERSTAY_GRACE_MIN   = int(os.getenv("OVERSTAY_GRACE_MIN", "5"))

# ============= ユーティリティ =============
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

_O_LOCK = threading.Lock()
_OCCUPANCY = 0
_METRICS = {"enter": 0, "exit": 0, "filtered": 0, "checkin_success": 0, "checkin_fail": 0}

def _snapshot_metrics():
    with _O_LOCK:
        return {
            "occupancy": _OCCUPANCY,
            "counters": dict(_METRICS),
            "bucket": AB_BUCKET,
        }

def _metrics_delta(after: dict, before: dict):
    da = {}
    ba = before.get("counters", {})
    aa = after.get("counters", {})
    for k, v in aa.items():
        da[k] = v - ba.get(k, 0)
    da["occupancy_delta"] = after.get("occupancy", 0) - before.get("occupancy", 0)
    return da

# --- No-show watcher state ---
_RSV_STATE = {}
# ★修正: デッドロック回避のため RLock (再帰ロック) を使用
_RS_LOCK = threading.RLock()

def _rsv_upsert(reservation_id, start_ts, end_ts):
    with _RS_LOCK:
        _RSV_STATE.setdefault(reservation_id, {
            "start": datetime.fromisoformat(start_ts) if isinstance(start_ts, str) else start_ts,
            "end":   datetime.fromisoformat(end_ts)   if isinstance(end_ts, str)   else end_ts,
            "checked_in": False,
            "auto_released": False,
            "closed": False,
            "overstayed": False
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
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        log_event("kpi_write_error", error=str(e), path=path)

class LineCounter:
    def __init__(self, axis="y", pos=360, dir_positive_is_enter=True,
                 iou_match=0.3, min_score=0.6, min_wh=20):
        self.axis = axis
        self.pos = pos
        self.sign = 1 if dir_positive_is_enter else -1
        self.iou_match = iou_match
        self.min_score = min_score
        self.min_wh = min_wh
        self.tracks = {}
        self._next_id = 1

    def update(self, det: dict):
        boxes = []
        for v in (det or {}).values():
            if not isinstance(v, dict) or "P" not in v: continue
            if v["P"] < self.min_score: continue
            if (v["x"] - v["X"] < self.min_wh) or (v["y"] - v["Y"] < self.min_wh): continue
            boxes.append(v)

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
                self.tracks.pop(tid, None)

        for i, b in enumerate(boxes):
            if i in assigned: continue
            cx, cy = _center(b)
            self.tracks[self._next_id] = {"bbox": b, "last_center": (cx, cy)}
            self._next_id += 1

        return events

def grant_lock(room_id: str) -> bool:
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

def checkin_attempt(room_id: str, ts_iso: str):
    if AB_BUCKET.lower().startswith("control"):
        log_event("ab_control_skip", room_id=room_id, ts=ts_iso)
        return None

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
                _record_checkin(rid)
                grant_lock(room_id)
                return rid
            else:
                log_event("checkin_attempt_nohit", room_id=room_id, ts=ts_iso)
                return None
        except Exception as e:
            log_event("rsv_checkin_error", room_id=room_id, error=str(e))
            return None

    if not MOCK_RESERVATION_MATCH:
        log_event("checkin_attempt_nohit", room_id=room_id, ts=ts_iso)
        return None
    
    # モック: 現在時刻でヒットする予約があるかチェック
    # 簡易的に内部関数を呼ぶか、ループで探す
    rid = None
    now_dt = datetime.fromisoformat(ts_iso)
    with _RS_LOCK:
        for r_id, st in _RSV_STATE.items():
             # 既にチェックイン済みでも複数人対応ならヒットさせる運用もありだが、ここではシンプルに
             # 「期間内ならヒット」とする
             w_start = st["start"] - timedelta(minutes=ARRIVAL_WINDOW_BEFORE_MIN)
             w_end   = st["end"]   + timedelta(minutes=NO_SHOW_GRACE_MIN)
             if w_start <= now_dt <= w_end:
                 rid = r_id
                 break
    
    if rid:
        log_event("checked_in", room_id=room_id, reservation_id=rid, ts=ts_iso)
        _record_checkin(rid)
        grant_lock(room_id)
        return rid
    else:
        log_event("checkin_attempt_nohit", room_id=room_id, ts=ts_iso)
        return None

def process_event(room_id: str, ev_type: str, ts_iso: str, count_delta: int, confidence: float):
    global _OCCUPANCY
    delta = 1 if ev_type == "enter" else (-1 if ev_type == "exit" else 0)

    if confidence < CONFIDENCE_MIN:
        with _O_LOCK:
            _METRICS["filtered"] += 1
            occ = _OCCUPANCY
        rec = {"ts": ts_iso, "event": "filtered", "room_id": room_id, "bucket": AB_BUCKET,
               "confidence": confidence, "occupancy": occ}
        log_event("filtered", room_id=room_id, conf=confidence)
        _kpi_write(rec)
        return

    rid = None
    with _O_LOCK:
        _OCCUPANCY = max(0, _OCCUPANCY + delta)
        occ_after = _OCCUPANCY
        if ev_type in ("enter", "exit"):
            _METRICS[ev_type] += 1

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
        log_event("edge_event", room_id=room_id, type=ev_type, ts=ts_iso,
                  count_delta=delta, confidence=confidence)

    kpi = {
        "ts": ts_iso, "room_id": room_id, "bucket": AB_BUCKET,
        "event": ev_type, "confidence": confidence,
        "delta": delta, "occupancy": occ_after,
    }
    if matched is not None:
        kpi["matched"] = matched
        if rid: kpi["reservation_id"] = rid
        kpi["arrival_window_before_min"] = ARRIVAL_WINDOW_BEFORE_MIN
        kpi["arrival_window_after_min"] = ARRIVAL_WINDOW_AFTER_MIN
        kpi["no_show_grace_min"] = NO_SHOW_GRACE_MIN

    _kpi_write(kpi)

def _normalize_to_repo_dict(payload: dict) -> dict:
    if not payload: return {}
    if isinstance(payload, dict) and any(isinstance(v, dict) and ("X" in v) for v in payload.values()):
        return {str(k): v for k, v in payload.items()}
    boxes = payload.get("boxes")
    if not isinstance(boxes, list): return {}
    fw = float(payload.get("frame_w", 1.0))
    fh = float(payload.get("frame_h", 1.0))
    out = {}
    for i, b in enumerate(boxes, start=1):
        if not isinstance(b, dict): continue
        P = float(b.get("score", b.get("P", 1.0)))
        if {"cx","cy","w","h"} <= b.keys():
            cx = float(b["cx"]); cy = float(b["cy"])
            w  = float(b["w"]);  h  = float(b["h"])
            if 0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0 and (fw>1 or fh>1):
                cx *= fw; cy *= fh; w *= fw; h *= fh
            l = cx - w/2.0; t = cy - h/2.0; r = cx + w/2.0; btm = cy + h/2.0
            out[str(i)] = {"X": l, "Y": t, "x": r, "y": btm, "P": P}
            continue
        if {"x","y","width","height"} <= b.keys():
            l = float(b["x"]); t = float(b["y"])
            w = float(b["width"]); h = float(b["height"])
            if 0.0 <= l <= 1.0 and 0.0 <= t <= 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0 and (fw>1 or fh>1):
                l *= fw; t *= fh; w *= fw; h *= fh
            out[str(i)] = {"X": l, "Y": t, "x": l+w, "y": t+h, "P": P}
            continue
        if {"left","top","right","bottom"} <= b.keys():
            out[str(i)] = {"X": float(b["left"]), "Y": float(b["top"]),
                           "x": float(b["right"]), "y": float(b["bottom"]), "P": P}
            continue
    return out

def edge_poller_loop():
    global LC
    if LC is None: LC = _make_line_counter()
    period = max(0.1, 1.0 / POLL_FPS)
    log_event("poller_started", room_id=ROOM_ID, fps=POLL_FPS, axis=LINE_AXIS, pos=LINE_POS)
    while True:
        try:
            det = ai.fetch_inference_result() or {}
            for ev in LC.update(det):
                ts = now_iso_jst()
                process_event(ROOM_ID, ev["type"], ts, 0, ev["confidence"])
            time.sleep(period)
        except Exception as e:
            log_event("poller_error", room_id=ROOM_ID, error=str(e))
            print(traceback.format_exc(), flush=True)
            time.sleep(1.0)
            continue

EDGE_SHARED_SECRET = os.getenv("EDGE_SHARED_SECRET")
def _require_edge_secret():
    if not EDGE_SHARED_SECRET: return None
    if request.headers.get("X-Edge-Secret") != EDGE_SHARED_SECRET:
        return jsonify(ok=False, error="forbidden"), 403
    return None

@app.get("/ping")
def ping(): return "pong"

@app.get("/healthz")
def healthz(): return jsonify(ok=True)

@app.get("/readyz")
def readyz():
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
        "room_id": ROOM_ID, "poll_fps": POLL_FPS, "line_axis": LINE_AXIS,
        "line_pos": LINE_POS, "dir_positive_is_enter": DIR_POSITIVE_IS_ENTER,
        "min_score": MIN_SCORE, "min_wh": MIN_WH, "iou_match": IOU_MATCH,
        "confidence_min": CONFIDENCE_MIN, "mock_reservation_match": MOCK_RESERVATION_MATCH,
    })

@app.get("/")
def root():
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
    LC = _make_line_counter()
    return jsonify(ok=True)

@app.post("/simulate/enter")
def simulate_enter():
    guard = _require_edge_secret()
    if guard is not None: return guard
    ts = now_iso_jst()
    process_event(ROOM_ID, "enter", ts, 0, confidence=0.95)
    return jsonify(ok=True, ts=ts)

@app.post("/simulate/frame")
def simulate_frame():
    guard = _require_edge_secret()
    if guard is not None: return guard
    global LC
    if LC is None: LC = _make_line_counter()
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
    guard = _require_edge_secret()
    if guard is not None: return guard
    global LC
    if LC is None: LC = _make_line_counter()
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
        time.sleep(0.02)
    return jsonify(ok=True, events=events, steps=steps)

def _simulate_visit_once(params: dict, room_id: str = ROOM_ID): # 引数追加
    global LC
    if LC is None: LC = _make_line_counter()
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
                cy2 = pos; cx2 = cx
            else:
                cx2 = pos; cy2 = cy
            det = {"1": {"X": cx2 - w / 2, "Y": cy2 - h / 2, "x": cx2 + w / 2, "y": cy2 + h / 2, "P": score}}
            ts = now_iso_jst()
            for ev in LC.update(det):
                # ★変更: 引数の room_id を使う
                process_event(room_id, ev["type"], ts, 0, ev["confidence"])
                evs.append(ev)
            time.sleep(0.02)
        return evs

    events = []
    events += _sweep(v0, v1)
    time.sleep(max(0, dwell_ms) / 1000.0)
    events += _sweep(v1, v0)
    return events, steps, dwell_ms

@app.post("/simulate/visit")
def simulate_visit():
    p = request.get_json(silent=True) or {}
    # ★追加: ボディに room_id があればそれを使い、なければ環境変数のデフォルトを使う
    target_room = p.get("room_id", ROOM_ID)

    before = _snapshot_metrics()
    # ★変更: _simulate_visit_once に room_id を渡せるようにするか、
    # 内部で process_event を呼ぶ際に target_room を使うように修正が必要
    # ここでは _simulate_visit_once 内の process_event 呼び出しを変えるため、
    # グローバル変数 ROOM_ID を一時的に無視して引数で渡す設計に変えるのが綺麗ですが、
    # 最小修正で済ますため _simulate_visit_once を少し改造します（下記参照）。
    events, steps, dwell_ms = _simulate_visit_once(p, target_room) # 引数追加
    after = _snapshot_metrics()
    ev_count = {"enter": 0, "exit": 0}
    for ev in events:
        t = ev.get("type")
        if t in ev_count: ev_count[t] += 1
    summary = {
        "bucket": AB_BUCKET, "visitors": 1, "events_count": ev_count,
        "metrics_before": before, "metrics_after": after, "metrics_delta": _metrics_delta(after, before),
    }
    return jsonify(ok=True, visitors=1, events=events, steps=steps, dwell_ms=dwell_ms, summary=summary)

@app.post("/simulate/scenario")
def simulate_scenario():
    """
    カメラ検知シナリオ（複数人の来訪）をまとめて流す API。
    body で room_id を指定した場合は、その部屋に対するイベントとして処理します。
    """
    p = request.get_json(silent=True) or {}
    pattern = str(p.get("pattern", "staggered")).lower()
    target_room = p.get("room_id", ROOM_ID)

    before = _snapshot_metrics()
    all_items = []
    total_ev_count = {"enter": 0, "exit": 0}

    visitors = int(p.get("visitors", p.get("num_visitors", 3)))
    visitors = max(1, visitors)
    gap_ms   = int(p.get("gap_ms", 500))
    min_d = int(p.get("min_dwell_ms", 500))
    max_d = int(p.get("max_dwell_ms", min_d))
    if max_d < min_d:
        max_d = min_d

    for i in range(visitors):
        vp = dict(p)
        vp["dwell_ms"] = random.randint(min_d, max_d)
        # ★ ここで room_id を明示的に渡す
        events, steps, used_dwell = _simulate_visit_once(vp, target_room)

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

        if i != visitors - 1:
            time.sleep(max(0, gap_ms) / 1000.0)

    after = _snapshot_metrics()
    summary = {
        "bucket": AB_BUCKET,
        "room_id": target_room,
        "pattern": pattern,
        "visitors": visitors,
        "events_count": total_ev_count,
        "metrics_before": before,
        "metrics_after": after,
        "metrics_delta": _metrics_delta(after, before),
    }
    return jsonify(ok=True, visitors=visitors, summary=summary, items=all_items)

@app.post("/simulate/reservation_scenario")
def simulate_reservation_scenario():
    """
    予約＋来訪シナリオ生成 API

    pattern:
      - "no_show" : 予約だけ作って全件ノーショー扱い（即 no_show_detected ログ）
      - "mixed"   : 来訪あり/なし混在（show_ratio で来訪率を制御）
      - "all_show": 全員来訪（全予約が来訪し、enter → 予約ヒットを狙う）
    """
    p = request.get_json(silent=True) or {}
    pattern = str(p.get("pattern", "no_show")).lower()
    if pattern not in ("no_show", "mixed", "all_show"):
        return jsonify(ok=False, error="unsupported pattern"), 400

    target_room = p.get("room_id", ROOM_ID)

    # 何件の予約を作るか
    num = int(p.get("num_reservations", p.get("count", 3)))
    if num < 1:
        num = 1

    duration_min = int(p.get("duration_min", 30))

    # start_offset_min は「今から何分後に開始する予約か」のベース
    # no_show 専用の時だけ「既に締め切りを過ぎた過去予約」として負のデフォルトにしておく
    if pattern == "no_show":
        default_offset = -(ARRIVAL_WINDOW_AFTER_MIN + NO_SHOW_GRACE_MIN + 5)
    else:
        default_offset = int(p.get("start_offset_min", 0))
    base_offset = int(p.get("start_offset_min", default_offset))

    id_prefix = str(p.get("id_prefix", "sim_ns_"))
    show_ratio = float(p.get("show_ratio", 0.5))  # mixed 用の来訪率

    created = []
    num_show = 0
    num_no_show = 0

    before = _snapshot_metrics()
    now_utc = datetime.now(timezone.utc)

    for i in range(num):
        rid = f"{id_prefix}{i+1}"

        # 予約開始・終了時刻（内部は UTC で保存）
        start_utc = now_utc + timedelta(minutes=base_offset + i)
        end_utc   = start_utc + timedelta(minutes=duration_min)

        # 予約状態を登録
        _rsv_upsert(rid, start_utc, end_utc)
        with _RS_LOCK:
            st = _RSV_STATE.get(rid)
            if st:
                st["room_id"] = target_room

        created.append({
            "reservation_id": rid,
            "room_id": target_room,
            "start_ts": start_utc.isoformat(),
            "end_ts": end_utc.isoformat(),
        })

        # パターンごとの処理
        if pattern == "no_show":
            # 全件ノーショー：即座に auto_released + no_show_detected ログ
            with _RS_LOCK:
                st = _RSV_STATE.get(rid)
                if st:
                    st["auto_released"] = True
            _kpi_write({
                "ts": now_iso_jst(),
                "room_id": target_room,
                "bucket": AB_BUCKET,
                "event": "no_show_detected",
                "reservation_id": rid,
                "arrival_window_after_min": ARRIVAL_WINDOW_AFTER_MIN,
                "no_show_grace_min": NO_SHOW_GRACE_MIN,
                "simulated": True,
            })
            num_no_show += 1
            continue

        # mixed / all_show の場合：この予約が「来訪するかどうか」を決める
        if pattern == "all_show":
            will_show = True
        else:  # mixed
            will_show = (random.random() < show_ratio)

        if will_show:
            # 来訪するケース：start_utc 付近の時刻を「チェックイン時刻」として enter イベントを流す
            # arrival_offset は到着時刻のランダムずれ（到着許容窓の範囲内）
            min_off = -ARRIVAL_WINDOW_BEFORE_MIN
            max_off = ARRIVAL_WINDOW_AFTER_MIN
            arrival_offset_min = random.randint(min_off, max_off)
            arrival_utc = start_utc + timedelta(minutes=arrival_offset_min)

            # process_event は ts_iso（JST文字列）を起点に checkin_attempt を呼ぶので、
            # ここで JST に変換して渡す
            ts_iso = arrival_utc.astimezone(JST).isoformat()
            process_event(target_room, "enter", ts_iso, 0, confidence=0.95)
            num_show += 1
        else:
            # 来訪しない：no_show_detected として扱う
            with _RS_LOCK:
                st = _RSV_STATE.get(rid)
                if st:
                    st["auto_released"] = True
            _kpi_write({
                "ts": now_iso_jst(),
                "room_id": target_room,
                "bucket": AB_BUCKET,
                "event": "no_show_detected",
                "reservation_id": rid,
                "arrival_window_after_min": ARRIVAL_WINDOW_AFTER_MIN,
                "no_show_grace_min": NO_SHOW_GRACE_MIN,
                "simulated": True,
            })
            num_no_show += 1

    after = _snapshot_metrics()

    return jsonify(
        ok=True,
        pattern=pattern,
        bucket=AB_BUCKET,
        room_id=target_room,
        num_reservations=num,
        num_show=num_show,
        num_no_show=num_no_show,
        metrics_before=before,
        metrics_after=after,
        metrics_delta=_metrics_delta(after, before),
        reservations=created,
    )

@app.post("/qr/checkin")
def qr_checkin():
    data = request.get_json(silent=True) or {}
    rid = data.get("reservation_id")
    ts = now_iso_jst()
    if MOCK_QR_REQUIRE_ID and not rid:
        matched = False
    else:
        matched = bool(rid)
        if RESERVATION_API_BASE:
            try:
                r = requests.post(f"{RESERVATION_API_BASE}/reservations/checkin-attempt",
                    json={"room_id": ROOM_ID, "ts": ts, "reservation_id": rid}, timeout=2)
                if r.ok:
                    jr = r.json()
                    matched = bool(jr.get("matched", matched))
                    rid = jr.get("reservation_id", rid)
            except Exception:
                pass
    if matched:
        with _O_LOCK: _METRICS["checkin_success"] += 1
        _record_checkin(rid)
        _kpi_write({"ts": ts, "room_id": ROOM_ID, "bucket": AB_BUCKET, "event": "qr_checkin", "matched": True, "reservation_id": rid, "method": "qr"})
        grant_lock(ROOM_ID)
        return jsonify(ok=True, matched=True, reservation_id=rid)
    else:
        with _O_LOCK: _METRICS["checkin_fail"] += 1
        _kpi_write({"ts": ts, "room_id": ROOM_ID, "bucket": AB_BUCKET, "event": "qr_checkin", "matched": False, "reservation_id": rid, "method": "qr"})
        return jsonify(ok=True, matched=False, reservation_id=rid), 404

@app.get("/metrics")
def metrics():
    with _O_LOCK:
        return jsonify({"occupancy": _OCCUPANCY, "counters": dict(_METRICS), "bucket": AB_BUCKET})

@app.get("/kpi/summary")
def kpi_summary():
    path = KPI_LOG_PATH
    if not path or not os.path.exists(path): return jsonify(ok=False, error="no_log"), 404
    enter = {}
    success = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception: continue
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
        e = enter.get(b, 0); s = success.get(b, 0)
        rate = (s / e) if e else 0.0
        out.append({"bucket": b, "enter": e, "success": s, "rate": rate})
    return jsonify(ok=True, summary=out)

@app.get("/kpi/tail")
def kpi_tail():
    n = int(request.args.get("n", 200))
    p = KPI_LOG_PATH
    if not p or not os.path.exists(p): return jsonify(ok=False, error="no_log"), 404
    lines = []
    with open(p, "r", encoding="utf-8") as f:
        buf = f.readlines()[-n:]
    for line in buf:
        try: lines.append(json.loads(line))
        except: pass
    return jsonify(ok=True, items=lines, count=len(lines))

@app.route("/kpi/no_show_summary", methods=["GET"])
def kpi_no_show_summary():
    total = 0
    buckets = {}
    try:
        with open(KPI_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try: row = json.loads(line)
                except Exception: continue
                if row.get("event") == "no_show_detected":
                    total += 1
                    b = row.get("bucket") or "unknown"
                    buckets[b] = buckets.get(b, 0) + 1
    except FileNotFoundError: pass
    return jsonify({"ok": True, "total_no_show": total, "by_bucket": buckets})

@app.get("/kpi/qr_summary")
def kpi_qr_summary():
    path = KPI_LOG_PATH
    if not path or not os.path.exists(path): return jsonify(ok=False, error="no_log"), 404
    per = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try: rec = json.loads(line)
            except: continue
            if rec.get("event") == "qr_checkin":
                b = rec.get("bucket","unknown")
                per.setdefault(b, {"qr_total":0,"qr_success":0})
                per[b]["qr_total"] += 1
                if rec.get("matched") is True: per[b]["qr_success"] += 1
    out = [{"bucket": b, "qr_total": v["qr_total"], "qr_success": v["qr_success"],
            "qr_rate": (v["qr_success"]/v["qr_total"] if v["qr_total"] else 0.0)} for b,v in per.items()]
    return jsonify(ok=True, summary=out)

@app.post("/kpi/reset_log")
def kpi_reset_log():
    path = KPI_LOG_PATH
    if not path: return jsonify(ok=False, error="no_log_path"), 400
    try:
        d = os.path.dirname(path)
        if d: os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f: pass
        return jsonify(ok=True)
    except Exception as e: return jsonify(ok=False, error=str(e)), 500

@app.post("/metrics/reset")
def metrics_reset():
    global _OCCUPANCY
    with _O_LOCK:
        _OCCUPANCY = 0
        for k in _METRICS.keys(): _METRICS[k] = 0
    return jsonify(ok=True)

# ============= モックAPI (Deadlock Fix: RLock使用) =============
@app.post("/mock/reservations/checkin-attempt")
def mock_rsv_checkin():
    payload = request.get_json(silent=True) or {}
    rid = payload.get("reservation_id")
    ts_str = payload.get("ts")
    # ★追加: リクエスト元の部屋ID
    req_room = payload.get("room_id", ROOM_ID)
    force = request.args.get("matched")

    if force is not None:
        matched = (force.lower() == "true")
        out_rid = rid or f"rsv_mock_forced_{int(time.time())}"
        return jsonify(matched=matched, reservation_id=out_rid, room_id=req_room)
    
    if rid:
        return jsonify(matched=True, reservation_id=rid, room_id=req_room)
    
    matched = False
    found_rid = None
    try:
        if ts_str: check_dt = datetime.fromisoformat(ts_str)
        else: check_dt = datetime.now(timezone.utc)
        
        with _RS_LOCK:
            for r_id, st in _RSV_STATE.items():
                # ★追加: 既にチェックイン済みなら無視 (1回限り)
                if st.get("checked_in"):
                    continue
                
                # ★追加: 部屋が違うなら無視
                if st.get("room_id", ROOM_ID) != req_room:
                    continue

                window_start = st["start"] - timedelta(minutes=ARRIVAL_WINDOW_BEFORE_MIN)
                window_end   = st["end"]   + timedelta(minutes=NO_SHOW_GRACE_MIN)
                if window_start <= check_dt <= window_end:
                    matched = True
                    found_rid = r_id
                    break
    except Exception:
        pass

    if matched and found_rid:
        return jsonify(matched=True, reservation_id=found_rid, room_id=req_room)
    else:
        return jsonify(matched=False, reservation_id=None, room_id=req_room)

@app.post("/mock/rooms/<room_id>/grant")
def mock_lock_grant(room_id):
    return jsonify(ok=True, room_id=room_id)

@app.route("/mock/reservations/create", methods=["POST"])
def mock_reservations_create():
    try:
        data = request.get_json(force=True) or {}
        rid = data.get("reservation_id") or f"rsv_{int(time.time())}"
        # ★追加: ルームIDを受け取る（指定なければデフォルト）
        target_room = data.get("room_id", ROOM_ID) 
        
        start_in = int(data.get("start_in_min", 3))
        duration = int(data.get("duration_min", 30))
        
        new_start = datetime.now(timezone.utc) + timedelta(minutes=start_in)
        new_end   = new_start + timedelta(minutes=duration)

        # ★追加: 過去時間のチェック (現在時刻より前ならエラー)
        if new_start < datetime.now(timezone.utc):
            return jsonify({"ok": False, "error": "past_time", "message": "過去の日時は指定できません"}), 400

        with _RS_LOCK:
            for existing_id, st in _RSV_STATE.items():
                if st.get("auto_released") or st.get("closed"): continue
                
                # ★追加: 部屋が違うなら重複チェック対象外
                if st.get("room_id", ROOM_ID) != target_room:
                    continue

                if (new_start < st["end"]) and (new_end > st["start"]):
                    return jsonify({
                        "ok": False, "error": "conflict", 
                        "message": f"予約重複({target_room}): {existing_id}"
                    }), 409
            
            # ★変更: room_id も一緒に保存する
            _rsv_upsert(rid, new_start, new_end)
            _RSV_STATE[rid]["room_id"] = target_room # upsert後にキー追加

        return jsonify({
            "ok": True, "reservation_id": rid, 
            "room_id": target_room,
            "start_ts": new_start.isoformat(), "end_ts": new_end.isoformat()
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/mock/reservations/upcoming", methods=["GET"])
def mock_reservations_upcoming():
    room_id = request.args.get("room_id") or ROOM_ID
    now = datetime.now(timezone.utc)
    until = now + timedelta(minutes=60) # 範囲は適当
    with _RS_LOCK:
        # 過去のものも含めて返す（タイムライン表示用）
        items = [
            {"reservation_id": rid, "room_id": room_id,
             "start_ts": st["start"].isoformat(), "end_ts": st["end"].isoformat(),
             "checked_in": st["checked_in"], "auto_released": st["auto_released"]}
            for rid, st in _RSV_STATE.items()
            # ★追加: 部屋IDが一致するものだけ返す
            if st.get("room_id", ROOM_ID) == room_id
        ]
    return jsonify({"ok": True, "reservations": items})

@app.route("/mock/reservations/all", methods=["GET"])
def mock_reservations_all():
    """
    全部屋分の予約一覧を返す簡易API。
    フロント側では「全室まとめの一覧表示」に利用します。
    """
    with _RS_LOCK:
        items = []
        for rid, st in _RSV_STATE.items():
            items.append({
                "reservation_id": rid,
                "room_id": st.get("room_id", ROOM_ID),
                "start_ts": st["start"].isoformat(),
                "end_ts": st["end"].isoformat(),
                "checked_in": st["checked_in"],
                "auto_released": st["auto_released"],
                "closed": st.get("closed", False),
                "overstayed": st.get("overstayed", False),
            })
    return jsonify({"ok": True, "reservations": items})

@app.route("/mock/reservations/auto-release", methods=["POST"])
def mock_auto_release():
    data = request.get_json(force=True) or {}
    rid = data.get("reservation_id")
    with _RS_LOCK:
        if rid in _RSV_STATE: _RSV_STATE[rid]["auto_released"] = True
    return jsonify({"ok": True, "reservation_id": rid, "action": "auto_released"})

@app.route("/reservations/extend", methods=["POST"])
def reservations_extend():
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
        st["closed"] = False
        st["overstayed"] = False
        room = st.get("room_id", ROOM_ID)

    _kpi_write({
        "ts": now_iso_jst(),
        "room_id": room,
        "bucket": AB_BUCKET,
        "event": "reservation_extended",
        "reservation_id": rid,
        "extend_min": add,
    })
    return jsonify(ok=True, reservation_id=rid, room_id=room, new_end_ts=st["end"].isoformat())

@app.route("/mock/locks/revoke", methods=["POST"])
def mock_locks_revoke():
    return jsonify({"ok": True})

# ============= UI =============
@app.get("/ui")
def ui_dashboard():
    # 既存のUI実装省略（変更なしとして省略するが、実際にはフルファイル提供が必要なら前の回答のものを維持）
    # ここでは簡易化せず、フルコードが必要との前提で、前のmain.pyのui_dashboard実装を使う
    lang = (request.args.get("lang") or "").lower()
    ja = (lang != "en")
    if ja:
        html = r"""<!doctype html><html lang="ja">
<head><meta charset="utf-8"><title>匿名チェックイン ダッシュボード</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script>
<style>.card{border:1px solid #e5e7eb;border-radius:1rem;padding:1rem}.grid{display:grid;gap:1rem}</style></head>
<body class="bg-gray-50 text-gray-900">
  <div class="max-w-6xl mx-auto p-6">
    <header class="mb-4"><div class="flex items-center justify-between"><h1 class="text-2xl font-bold">匿名チェックイン ダッシュボード</h1><div class="text-sm text-gray-500" id="roomMeta"></div></div>
    <nav class="mt-2 text-sm flex flex-wrap gap-3"><a class="text-indigo-600 underline" href="/ui/qr">QRチェックイン</a><a class="text-indigo-600 underline" href="/ui/dev">開発者ツール</a><a class="text-indigo-600 underline" href="/ui/signage" target="_blank">掲示</a></nav></header>
    <section class="grid md:grid-cols-4 grid-cols-2">
      <div class="card"><div class="text-sm text-gray-500">在室人数</div><div class="text-3xl font-semibold" id="occ">–</div></div>
      <div class="card"><div class="text-sm text-gray-500">A/Bバケット</div><div class="text-xl font-medium" id="bucket">–</div></div>
      <div class="card"><div class="text-sm text-gray-500">入室 / 退室</div><div class="text-xl" id="ee">– / –</div></div>
      <div class="card"><div class="text-sm text-gray-500">QR成功 / 失敗</div><div class="text-xl" id="qr">– / –</div></div>
    </section>
    <section class="mt-6 grid md:grid-cols-3"><div class="card"><div class="font-semibold mb-2">A/B KPI</div><table class="w-full text-sm"><tbody id="kpiSummary"></tbody></table></div>
    <div class="card"><div class="font-semibold mb-2">QRチェックイン</div><table class="w-full text-sm"><tbody id="qrSummary"></tbody></table></div>
    <div class="card"><div class="font-semibold mb-2">ノーショー</div><div class="text-sm">合計: <span id="noShowTotal">0</span></div><table class="w-full text-sm mt-2"><tbody id="noShowPer"></tbody></table></div></section>
  </div>
<script>
const fmtPct = x => (x===0? "0.0%" : (x? (x*100).toFixed(1)+"%" : "–"));
async function getJSON(url){ try{ const r=await fetch(url); if(!r.ok) throw new Error(r.status); return await r.json(); }catch{ return null; } }
async function refresh(){
  const m = await getJSON('/metrics');
  if(m){ document.getElementById('occ').textContent=m.occupancy??'–'; document.getElementById('bucket').textContent=m.bucket??'–'; 
         document.getElementById('ee').textContent=(m.counters.enter??0)+'/'+(m.counters.exit??0); document.getElementById('qr').textContent=(m.counters.checkin_success??0)+'/'+(m.counters.checkin_fail??0); 
         document.getElementById('roomMeta').textContent=`A/B=${m.bucket}`; }
  const k = await getJSON('/kpi/summary'); const tbody = document.getElementById('kpiSummary'); tbody.innerHTML='';
  if(k&&k.summary) k.summary.forEach(r=>{ tbody.innerHTML+=`<tr><td>${r.bucket}</td><td class="text-right">${r.enter}</td><td class="text-right">${r.success}</td><td class="text-right">${fmtPct(r.rate)}</td></tr>`; });
  const q = await getJSON('/kpi/qr_summary'); const qtb = document.getElementById('qrSummary'); qtb.innerHTML='';
  if(q&&q.summary) q.summary.forEach(r=>{ qtb.innerHTML+=`<tr><td>${r.bucket}</td><td class="text-right">${r.qr_total}</td><td class="text-right">${r.qr_success}</td><td class="text-right">${fmtPct(r.qr_rate)}</td></tr>`; });
  const n = await getJSON('/kpi/no_show_summary'); document.getElementById('noShowTotal').textContent=n?.total_no_show??0;
  const ns=document.getElementById('noShowPer'); ns.innerHTML=''; if(n&&n.by_bucket) Object.entries(n.by_bucket).forEach(([b,c])=>{ ns.innerHTML+=`<tr><td>${b}</td><td class="text-right">${c}</td></tr>`; });
}
refresh(); setInterval(refresh, 3000);
</script></body></html>"""
    else:
        html = "<html><body>English UI not implemented fully here.</body></html>"
    return render_template_string(html)

@app.get("/ui/qr")
def ui_qr_manual():
    html = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>QRチェックイン</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-50 text-gray-900"><div class="max-w-3xl mx-auto p-6"><h1 class="text-2xl font-bold mb-4">QRチェックイン</h1>
<input id="rid" type="text" placeholder="予約ID" class="border p-2 rounded w-full mb-2"><button id="btn" class="bg-indigo-600 text-white p-2 rounded">チェックイン</button>
<div id="msg" class="mt-2"></div></div>
<script>document.getElementById('btn').onclick=async()=>{
const rid=document.getElementById('rid').value;
const r=await fetch('/qr/checkin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reservation_id:rid})});
const j=await r.json(); document.getElementById('msg').textContent=r.ok?('成功: '+j.reservation_id):('失敗');
}</script></body></html>"""
    return render_template_string(html)

@app.get("/ui/dev")
def ui_dev():
    html = r"""
<!doctype html>
<html lang="ja"><head>
  <meta charset="utf-8"><title>開発者ツール (Full Fixed)</title>
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
  <header class="flex items-center justify-between"><h1 class="text-2xl font-bold">開発者ツール <span class="text-sm font-normal text-gray-500 ml-2">with Timeline</span></h1><nav class="text-sm"><a class="text-indigo-600 underline" href="/ui">← ダッシュボードへ</a></nav></header>

  <section class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="card"><div class="text-sm text-gray-500">在室人数</div><div class="text-3xl font-semibold" id="occ">–</div></div>
    <div class="card"><div class="text-sm text-gray-500">A/Bバケット</div><div class="text-xl font-medium" id="bucket">–</div></div>
    <div class="card"><div class="text-sm text-gray-500">入室 / 退室</div><div class="text-xl" id="ee">– / –</div></div>
    <div class="card"><div class="text-sm text-gray-500">QR成功 / 失敗</div><div class="text-xl" id="qr">– / –</div></div>
  </section>
  <hr class="border-gray-300">

  <div class="flex items-center gap-2 bg-white p-3 rounded border">
  <span class="text-sm font-bold">Target Room:</span>
  <select id="targetRoom" class="border rounded px-2 py-1 text-sm bg-gray-50" onchange="loadUpcoming()">
    <option value="R-0001" selected>Room 01 (R-0001)</option>
    <option value="R-0002">Room 02 (R-0002)</option>
    <option value="R-0003">Room 03 (R-0003)</option>
    <option value="R-0004">Room 04 (R-0004)</option>
  </select>
  <span class="text-xs text-gray-400">※予約作成やシミュレーションはこの部屋に対して実行されます</span>
</div>

  <section class="bg-white border rounded-xl p-4 shadow-sm">
    <div class="flex justify-between items-center mb-2"><h2 class="font-bold text-lg">部屋予約状況 (Timeline)</h2><div class="text-xs text-gray-500">現在時刻: <span id="clock" class="font-mono">--:--:--</span></div></div>
    <div class="relative">
        <div class="flex justify-between text-xs text-gray-400 mb-1"><span>Now - 30min</span><span>Now</span><span>Now + 60min</span></div>
        <div id="timeline" class="timeline-container"><div class="timeline-now" style="left: 33.3%;"></div></div>
    </div>
    <details class="mt-2"><summary class="text-xs text-indigo-600 cursor-pointer">予約リスト(Raw JSON)を表示</summary><pre id="rsv" class="text-xs bg-gray-50 rounded p-2 mt-1 overflow-auto h-24 border"></pre></details>
    <div class="mt-4 bg-gray-50 p-3 rounded border">
      <div class="mt-3">
    <div class="text-xs font-semibold mb-1">全室の予約一覧</div>
    <div id="rsvAllTable" class="text-xs bg-gray-50 rounded p-2 h-24 overflow-auto border">
      予約なし
    </div>
  </div>
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
         <button class="flex-1 py-1 bg-gray-600 text-white rounded" onclick="runMultiForBucket('treatment')">treatment</button>
         <button class="flex-1 py-1 bg-gray-600 text-white rounded" onclick="runMultiForBucket('control')">control</button>
      </div>
      <pre id="scenarioOut" class="text-xs bg-gray-50 rounded p-2 h-20 overflow-auto mt-2 border"></pre>
    </div>
    <div class="space-y-4">
<div class="bg-white border rounded-xl p-4">
  <div class="font-semibold mb-2 text-sm">予約＋来訪シナリオ</div>
  <div class="flex flex-wrap items-end gap-2">
    <label class="text-xs">
      件数
      <input id="rsvNum" type="number" value="3" class="border rounded px-1 w-12">
    </label>
    <label class="text-xs">
      Prefix
      <input id="rsvPrefix" type="text" value="sim_ns_" class="border rounded px-1 w-20">
    </label>
    <label class="text-xs">
      来訪率(0〜1, mixed用)
      <input id="rsvShowRatio" type="number" value="0.5" step="0.1" min="0" max="1"
             class="border rounded px-1 w-16">
    </label>
  </div>
  <div class="flex mt-2 gap-2 text-xs">
    <button class="px-3 py-2 bg-rose-600 text-white rounded flex-1 shadow"
            onclick="runReservationScenario('no_show')">ノーショーのみ</button>
    <button class="px-3 py-2 bg-amber-500 text-white rounded flex-1 shadow"
            onclick="runReservationScenario('mixed')">混在</button>
    <button class="px-3 py-2 bg-emerald-600 text-white rounded flex-1 shadow"
            onclick="runReservationScenario('all_show')">全員来訪</button>
  </div>
  <div id="rsvScenarioOut" class="mt-1 text-xs text-gray-500 break-words"></div>
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
             Window: <input id="arr_b" class="border w-8" value="10"><input id="arr_a" class="border w-8" value="15"><input id="grace" class="border w-8" value="7">
             <button class="bg-blue-600 text-white px-2 rounded" onclick="updateWindow()">Set</button>
           </div>
      </div>
      <div class="flex flex-wrap gap-2 pt-2 border-t">
        <button class="px-3 py-1 bg-gray-200 rounded text-xs" onclick="setBucket('treatment')">Treatment</button>
        <button class="px-3 py-1 bg-gray-200 rounded text-xs" onclick="setBucket('control')">Control</button>
        <button class="px-3 py-1 bg-gray-700 text-white rounded text-xs" onclick="resetMetrics()">カウンタReset</button>
        <button class="px-3 py-1 bg-rose-600 text-white rounded text-xs" onclick="resetKpiLog()">KPIログReset</button>
      </div>
      <div id="adminMsg" class="text-xs text-gray-500 mt-1"></div>
  </section>
  <section class="bg-white border rounded-xl p-4">
    <div class="flex justify-between items-center mb-1"><div class="font-semibold text-sm">KPIログ末尾</div><button class="text-xs bg-gray-200 px-2 py-1 rounded" onclick="loadTail()">更新</button></div>
    <pre id="tail" class="text-xs bg-gray-50 rounded p-3 overflow-auto h-32 border"></pre>
  </section>
</div>
<script>
let cachedReservations = [];
let allReservations = [];  // ★追加: 全室分

async function j(u,m='GET',b){
  const o={method:m,headers:{'Content-Type':'application/json'}};
  if(b)o.body=JSON.stringify(b);
  const r=await fetch(u,o);
  if(!r.ok) { let t=r.statusText; try{const j=await r.json();if(j.message)t=j.message;else if(j.error)t=j.error}catch{} throw new Error(`[${r.status}] ${t}`); }
  try{return await r.json()}catch{return {}}
}
function setText(id,t){const el=document.getElementById(id);if(el)el.textContent=(typeof t==='string'?t:JSON.stringify(t,null,2));}
const fmtPct = x => (x===0? "0.0%" : (x? (x*100).toFixed(1)+"%" : "–"));

let cachedReservations = [];
// 予約一覧取得 (タイムライン用)
async function loadUpcoming(){
  try {
    // ★変更: room_idパラメータを付与
    const room = document.getElementById('targetRoom').value;
    const r = await j('/mock/reservations/upcoming?room_id=' + room);
    if(r.reservations) {
       cachedReservations = r.reservations;
       // 生JSON表示も更新
       setText('rsv', cachedReservations);
    }
    renderTimeline();
  } catch(e) { console.error(e); }
}
function renderTimeline(){
  const el=document.getElementById('timeline');
  el.innerHTML='<div class="timeline-now" style="left: 33.3%;"></div>';
  const now=new Date();
  const startWin=new Date(now.getTime()-30*60000);
  const totalMs=90*60000;
  cachedReservations.forEach(r=>{
    const start=new Date(r.start_ts); const end=new Date(r.end_ts);
    const leftPct=((start-startWin)/totalMs)*100;
    const widthPct=((end-start)/totalMs)*100;
    if(leftPct+widthPct<0 || leftPct>100) return;
    const bar=document.createElement('div'); bar.className='timeline-bar';
    if(r.checked_in) bar.classList.add('timeline-checkin');
    if(r.auto_released) bar.classList.add('timeline-noshow');
    bar.style.left=Math.max(0,leftPct)+'%';
    bar.style.width=Math.min(100-Math.max(0,leftPct),widthPct)+'%';
    bar.innerText=r.reservation_id;
    el.appendChild(bar);
  });
  document.getElementById('clock').innerText=now.toLocaleTimeString();
}
async function loadAllReservations(){
  try {
    const r = await j('/mock/reservations/all');
    allReservations = r.reservations || [];
    renderAllReservations();
  } catch(e) {
    console.error(e);
  }
}

function renderAllReservations(){
  const el = document.getElementById('rsvAllTable');
  if (!el) return;

  if (!allReservations.length){
    el.textContent = '予約なし';
    return;
  }

  let html = '<table class="w-full"><thead><tr>' +
             '<th class="text-left">Room</th>' +
             '<th class="text-left">ID</th>' +
             '<th class="text-right">開始</th>' +
             '<th class="text-right">終了</th>' +
             '<th class="text-right">状態</th>' +
             '</tr></thead><tbody>';

  allReservations.forEach(r => {
    const st = [];
    if (r.checked_in)    st.push('入室済');
    if (r.auto_released) st.push('ノーショー解除');
    if (r.closed)        st.push('終了');
    if (r.overstayed)    st.push('延長超過');

    html += `<tr>
      <td>${r.room_id}</td>
      <td>${r.reservation_id}</td>
      <td class="text-right">${new Date(r.start_ts).toLocaleTimeString()}</td>
      <td class="text-right">${new Date(r.end_ts).toLocaleTimeString()}</td>
      <td class="text-right">${st.join(',') || '-'}</td>
    </tr>`;
  });

  html += '</tbody></table>';
  el.innerHTML = html;
}

// 予約作成
async function createCustomRsv(){
  const rid = document.getElementById('newRid').value;
  const start = Number(document.getElementById('newStart').value);
  const dur = Number(document.getElementById('newDur').value);
  // ★追加: 選択中のルームIDを取得
  const room = document.getElementById('targetRoom').value; 
  
  const msg = document.getElementById('rsvMsg');
  msg.className = "text-xs p-2 rounded font-bold hidden"; msg.textContent = "";

  try {
    // ★変更: room_id を送信
    const res = await j('/mock/reservations/create','POST',{
        reservation_id: rid, 
        room_id: room,          // <--- 追加
        start_in_min: start, 
        duration_min: dur
    });
    msg.textContent = "作成成功: " + (res.reservation_id || rid) + " @ " + room;
    msg.className = "text-xs p-2 rounded font-bold bg-emerald-100 text-emerald-700 block";
    await loadUpcoming();
    // ... (IDインクリメント処理)
  } catch(e) {
    msg.textContent = "作成失敗: " + e.message;
    msg.className = "text-xs p-2 rounded font-bold bg-rose-100 text-rose-700 block";
  }
}
async function clearReservations(){ alert("リロードします"); location.reload(); }
async function opExtend(){
  const rid=document.getElementById('opRid').value.trim(), ext=Number(document.getElementById('opExt').value)||15;
  try{ const r=await j('/reservations/extend','POST',{reservation_id:rid, extend_min:ext}); setText('opOut',"延長成功: "+r.new_end_ts); loadUpcoming(); }catch(e){ setText('opOut',"失敗: "+e.message); }
}
async function setBucket(b){ const r=await j('/ab/set','POST',{bucket:b}); setText('adminMsg',`Bucket: ${r.bucket}`); refreshMonitor(); }
async function updateCfg(){
  const p={line_axis:document.getElementById('line_axis').value, line_pos:Number(document.getElementById('line_pos').value), min_score:Number(document.getElementById('min_score').value), min_wh:Number(document.getElementById('min_wh').value), iou_match:Number(document.getElementById('iou_match').value), confidence_min:Number(document.getElementById('conf_min').value)};
  await j('/config/update','POST',p); setText('adminMsg','Config Updated');
}
async function updateWindow(){ await j('/config/update','POST',{arrival_before_min:Number(document.getElementById('arr_b').value), arrival_after_min:Number(document.getElementById('arr_a').value), no_show_grace_min:Number(document.getElementById('grace').value)}); setText('adminMsg','Window Updated'); }
async function resetMetrics(){ await j('/metrics/reset','POST',{}); refreshMonitor(); }
async function resetKpiLog(){ await j('/kpi/reset_log','POST',{}); refreshMonitor(); }
// シミュレーション実行 (単発)
async function runSingle(){ 
  try{ 
    const room = document.getElementById('targetRoom').value;
    // ★変更: room_id を送信
    const r = await j('/simulate/visit', 'POST', {
        dwell_ms: 800, 
        room_id: room    // <--- 追加
    }); 
    setText('scenarioOut', r); 
    refreshMonitor(); 
  } catch(e){ setText('scenarioOut', e.message) } 
}
// ランダムシミュレーション
async function runMulti(){ 
  try{ 
    const room = document.getElementById('targetRoom').value;
    // ★変更: scenario API は内部で simulate_visit を呼ぶため、そこへパラメータを渡す必要がある
    // (※ simulate_scenario 側でも room_id を受け取って回すよう修正が必要ですが、
    //  簡易的には visit 単位でパラメータを指定します)
    const r = await j('/simulate/scenario', 'POST', {
        visitors: 5,
        room_id: room    // <--- 追加
    }); 
    setText('scenarioOut', r); 
    refreshMonitor(); 
  } catch(e){ setText('scenarioOut', e.message) } 
}
async function runMultiForBucket(b){ await j('/ab/set','POST',{bucket:b}); runMulti(); }
async function runReservationScenario(mode){
  const n   = Number(document.getElementById('rsvNum').value) || 3;
  const pre = document.getElementById('rsvPrefix').value || 'sim_ns_';
  const room = document.getElementById('targetRoom').value;
  const ratio = Number(document.getElementById('rsvShowRatio').value) || 0.5;

  const payload = {
    pattern: mode,
    num_reservations: n,
    id_prefix: pre,
    room_id: room,
  };
  if (mode === 'mixed') {
    payload.show_ratio = ratio;
  }

  try {
    const r = await j('/simulate/reservation_scenario','POST', payload);

    // ちょっと読みやすい要約テキストにする
    const d = r.metrics_delta || {};
    const enterDelta  = (d.enter ?? 0);
    const hitDelta    = (d.checkin_success ?? 0);
    const occDelta    = (d.occupancy_delta ?? 0);

    const summary =
      `pattern=${r.pattern}, room=${r.room_id}, ` +
      `total=${r.num_reservations}, show=${r.num_show||0}, no_show=${r.num_no_show||0} / ` +
      `Δenter=${enterDelta}, Δcheckin_hit=${hitDelta}, Δocc=${occDelta}`;

    setText('rsvScenarioOut', summary);
    await refreshMonitor();
  } catch(e){
    setText('rsvScenarioOut', "エラー: " + e.message);
  }
}

async function refreshMonitor(){
  const m=await j('/metrics'); 
  if(m && m.counters){
    setText('occ',m.occupancy??'–');
    setText('bucket',m.bucket??'–');
    setText('ee',(m.counters.enter??0)+'/'+(m.counters.exit??0));
    setText('qr',(m.counters.checkin_success??0)+'/'+(m.counters.checkin_fail??0));
  }

  const k=await j('/kpi/summary');
  const mk=document.getElementById('miniKpi');
  if(mk){
    let h='<table class="w-full"><tr><td>Bucket</td><td align="right">Ent</td><td align="right">Hit</td></tr>';
    (k.summary||[]).forEach(r=>{
      h+=`<tr><td>${r.bucket}</td><td align="right">${r.enter}</td><td align="right">${r.success} (${fmtPct(r.rate)})</td></tr>`;
    });
    h+='</table>'; mk.innerHTML=h;
  }

  await loadUpcoming();        // 選択中の1部屋（タイムライン）
  await loadAllReservations(); // ★追加: 全室一覧
}

async function loadTail(){ const r=await j('/kpi/tail?n=50'); setText('tail',r.items); }
// 初期表示
refreshMonitor();    // メトリクス＋予約情報読み込み
loadTail();          // KPIログ末尾
setInterval(refreshMonitor, 1000);
</script></body></html>"""
    return render_template_string(html)

def no_show_watcher_loop():
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
                    with _RS_LOCK:
                        st["auto_released"] = True
                    room = st.get("room_id", ROOM_ID)

                    _kpi_write({
                        "ts": now_iso_jst(),
                        "room_id": room,
                        "bucket": AB_BUCKET,
                        "event": "no_show_detected",
                        "reservation_id": rid,
                        "arrival_window_after_min": ARRIVAL_WINDOW_AFTER_MIN,
                        "no_show_grace_min": NO_SHOW_GRACE_MIN,
                    })

                    if RESERVATION_API_BASE:
                        try:
                            requests.post(
                                f"{RESERVATION_API_BASE}/reservations/auto-release",
                                json={"reservation_id": rid, "room_id": room},
                                timeout=2,
                            )
                        except Exception:
                            pass
                    if LOCK_API_BASE:
                        try:
                            requests.post(
                                f"{LOCK_API_BASE}/locks/revoke",
                                json={"reservation_id": rid, "room_id": room},
                                timeout=2,
                            )
                        except Exception:
                            pass
        except Exception as e:
            app.logger.exception("no_show_watcher error: %s", e)
        time.sleep(interval_sec)

def end_watcher_loop():
    interval_sec = 30
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            with _RS_LOCK:
                items = list(_RSV_STATE.items())
            for rid, st in items:
                if st["auto_released"]:
                    continue

                room = st.get("room_id", ROOM_ID)

                close_from = st["end"] - timedelta(minutes=END_CLOSE_WINDOW_MIN)
                close_to   = st["end"] + timedelta(minutes=END_CLOSE_WINDOW_MIN)

                # 終了ウィンドウ内で在室0なら「終了」
                if (not st.get("closed")) and (close_from <= now_utc <= close_to):
                    with _O_LOCK:
                        occ = _OCCUPANCY
                    if occ == 0:
                        st["closed"] = True
                        _kpi_write({
                            "ts": now_iso_jst(),
                            "room_id": room,
                            "bucket": AB_BUCKET,
                            "event": "reservation_closed",
                            "reservation_id": rid,
                            "reason": "zero_occupancy_near_end",
                        })
                        if LOCK_API_BASE:
                            try:
                                requests.post(
                                    f"{LOCK_API_BASE}/locks/revoke",
                                    json={"reservation_id": rid, "room_id": room},
                                    timeout=2,
                                )
                            except Exception:
                                pass

                # 終了＋猶予を超えてまだ在室>0なら「オーバーステイ」
                deadline = st["end"] + timedelta(minutes=OVERSTAY_GRACE_MIN)
                if (not st.get("overstayed")) and (now_utc >= deadline):
                    with _O_LOCK:
                        occ = _OCCUPANCY
                    if occ > 0:
                        st["overstayed"] = True
                        _kpi_write({
                            "ts": now_iso_jst(),
                            "room_id": room,
                            "bucket": AB_BUCKET,
                            "event": "overstay_detected",
                            "reservation_id": rid,
                            "occupancy": occ,
                        })
                        if RESERVATION_API_BASE:
                            try:
                                requests.post(
                                    f"{RESERVATION_API_BASE}/reservations/overstay",
                                    json={"reservation_id": rid, "room_id": room, "occupancy": occ},
                                    timeout=2,
                                )
                            except Exception:
                                pass
        except Exception as e:
            app.logger.exception("end_watcher error: %s", e)
        time.sleep(interval_sec)

def _public_urls(host: str, port: int, path: str) -> list[str]:
    urls = []
    override = os.getenv("PUBLIC_URL_BASE")
    if override: urls.append(override.rstrip("/") + path)
    if os.environ.get("CODESPACES") == "true":
        name = os.environ.get("CODESPACE_NAME")
        domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
        if name:
            urls.append(f"https://{name}-{port}.{domain}{path}")
            urls.append(f"https://{port}-{name}.{domain}{path}")
    gp = os.environ.get("GITPOD_WORKSPACE_URL")
    if gp:
        base = gp.rstrip("/").replace("https://", "")
        urls.append(f"https://{base.split('/')[0].split('.')[0]}-{port}.{'.'.join(base.split('.')[1:])}{path}")
        urls.append(gp.replace("https://", f"https://{port}-") + path)
    urls.append(f"http://{host}:{port}{path}")
    seen = set()
    uniq = []
    for u in urls:
        if u not in seen:
            uniq.append(u)
            seen.add(u)
    return uniq

def _auto_open_ui(host="127.0.0.1", port=8000, path="/ui"):
    if os.environ.get("AUTO_OPEN_UI", "1") != "1": return
    candidates = _public_urls(host, port, path)
    def _try_open(urls: list[str]):
        chosen = None
        for u in urls:
            try:
                time.sleep(0.3)
                r = requests.get(u, timeout=2, allow_redirects=True)
                if r.status_code != 404:
                    chosen = u
                    break
            except Exception: continue
        if chosen is None: chosen = urls[0]
        try: webbrowser.open(chosen, new=2)
        except Exception as e: print(f"[ui] auto open failed: {e}", flush=True)
    threading.Timer(0.8, _try_open, args=(candidates,)).start()

def _start_threads():
    threading.Thread(target=no_show_watcher_loop, daemon=True, name="no_show_watcher").start()
    threading.Thread(target=end_watcher_loop,    daemon=True, name="end_watcher").start()
    if not DISABLE_POLLER:
        threading.Thread(target=edge_poller_loop, daemon=True, name="edge_poller").start()

if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _start_threads()
        _auto_open_ui("127.0.0.1", 8000, "/ui")
    app.run(host="0.0.0.0", port=8000, debug=True)