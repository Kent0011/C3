了解です！
今の `main.py` がかなり育ってきたので、**「このコード全体が何をしていて、どの関数がどの役割か」**をできるだけ丁寧に整理して説明します。

> ✅ 方針
>
> * 行単位ではなく「かたまり（モジュール）」ごとに説明
> * あなたの要件（無人ワークスペース / 予約 / ノーショー / A/B テスト / シナリオデバッグ）と対応づけて解説
> * 「ここを触ると何が起きるか」「GUI とどうつながるか」も書く

---

## 1. 全体のざっくり構成

`main.py` は大きく分けると、こんなパーツです：

1. **環境セットアップ & 時刻(JST/UTC)**
2. **グローバル設定（room_id, ライン位置, 予約窓など）**
3. **在室人数 & KPIカウンタの管理**
4. **予約状態 `_RSV_STATE` & ノーショー/終了/オーバーステイ監視**
5. **`LineCounter`：AIカメラのバウンディングボックス → enter/exit 判定**
6. **`process_event`：enter/exit イベントから在室更新・予約ヒット・KPIログ**
7. **シミュレーション用 API 群（/simulate/…）**
8. **モック予約・鍵API群（/mock/… , /reservations/extend）**
9. **KPI・メトリクス API（/metrics, /kpi/…）**
10. **UIエンドポイント（/ui, /ui/qr, /ui/dev）＋フロントのJS**
11. **バックグラウンドスレッド（no-show監視・終了監視・カメラポーラ）**
12. **起動処理 & GitHub Codespaces / ローカルブラウザ自動オープン**

以下、それぞれ詳しく。

---

## 2. 環境セットアップ & 時刻まわり

### .env と Flask アプリ

```python
load_dotenv()
app = Flask(__name__)
ai = Repository.AiCameraRepository(...)

os.environ.setdefault("TZ", "Asia/Tokyo")
try:
    time.tzset()
except Exception:
    pass

JST = timezone(timedelta(hours=9))
def now_iso_jst() -> str:
    return datetime.now(JST).isoformat()
```

* `.env` から **カメラ/予約APIの接続情報** を読み込み。
* `TZ=Asia/Tokyo` を環境変数でセットし、`time.tzset()` できる環境なら**プロセスのローカルタイムを日本時間に揃える**。
* ただし、**内部で重要な予約時刻は UTC (`datetime.now(timezone.utc)`) で保存**しており、UI 表示やログの `ts` だけ `now_iso_jst()` = JST 文字列で書いています。

  * つまり：

    * **予約状態 `_RSV_STATE` → UTC**
    * **ログやUIに出る文字列 `ts` → JST**

---

## 3. グローバル設定（環境変数の反映）

```python
ROOM_ID = os.getenv("ROOM_ID", "R-0001")
POLL_FPS = float(os.getenv("POLL_FPS", "2.0"))
LINE_AXIS = os.getenv("LINE_AXIS", "y")
LINE_POS = int(os.getenv("LINE_POS", "360"))
DIR_POSITIVE_IS_ENTER = (os.getenv("DIR_POSITIVE_IS_ENTER", "1") == "1")
...
ARRIVAL_WINDOW_BEFORE_MIN = int(os.getenv("ARRIVAL_WINDOW_BEFORE_MIN", "10"))
ARRIVAL_WINDOW_AFTER_MIN  = int(os.getenv("ARRIVAL_WINDOW_AFTER_MIN", "15"))
NO_SHOW_GRACE_MIN         = int(os.getenv("NO_SHOW_GRACE_MIN", "7"))
...
END_CLOSE_WINDOW_MIN = int(os.getenv("END_CLOSE_WINDOW_MIN", "5"))
OVERSTAY_GRACE_MIN   = int(os.getenv("OVERSTAY_GRACE_MIN", "5"))
```

* **ライン検知のパラメータ**

  * `LINE_AXIS`: `y` なら上下移動でライン交差を判定、`x` なら左右
  * `LINE_POS`: 画面上のライン位置 (ピクセル)
  * `DIR_POSITIVE_IS_ENTER`: 境界をどちら方向に跨いだら「enter」とみなすか
  * `MIN_SCORE`, `MIN_WH`, `IOU_MATCH`：検出品質フィルタ

* **A/B テスト**

  * `AB_BUCKET` = "treatment" or "control"
  * `MOCK_RESERVATION_MATCH` = True ならモック予約ヒット機構をON

* **ノーショー関連**

  * `ARRIVAL_WINDOW_BEFORE_MIN`: 予約開始時刻の何分前から到着OKか
  * `ARRIVAL_WINDOW_AFTER_MIN`: 開始時刻の何分後まで遅刻OKか
  * `NO_SHOW_GRACE_MIN`: さらにどれだけ待ってから「ノーショー」と判定するか

* **終了/オーバーステイ関連**

  * `END_CLOSE_WINDOW_MIN`: 予約終了時刻前後の「終了判定ウィンドウ」
  * `OVERSTAY_GRACE_MIN`: 終了後どれだけ猶予をみてから「超過利用」とみなすか

GUI `/ui/dev` の「Window」欄の入力はこの値を `/config/update` 経由で上書きしています。

---

## 4. 在室人数 & KPIカウンタ

```python
_O_LOCK = threading.Lock()
_OCCUPANCY = 0
_METRICS = {
    "enter": 0, "exit": 0, "filtered": 0,
    "checkin_success": 0, "checkin_fail": 0
}
```

* `_OCCUPANCY`: **現在の部屋の在室人数（単一カウンタ）**
* `_METRICS`:

  * `enter` / `exit`: ライン通過からカウントした入退室イベント数
  * `filtered`: 信頼度不足で弾いたイベント数（`confidence < CONFIDENCE_MIN`）
  * `checkin_success`: 到着時に予約マッチに成功した回数
  * `checkin_fail`   : 到着時に予約マッチできなかった回数

これを返すのが `/metrics`。
`/ui` と `/ui/dev` の上部カード（在室人数・入室/退室・QR成功/失敗）は、この `/metrics` を 1〜3秒ごとに輪読して表示しています。

---

## 5. 予約状態 `_RSV_STATE` とノーショー・終了・オーバーステイ

### 予約状態の保存

```python
_RSV_STATE = {}
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
```

* `_RSV_STATE` は **全予約の状態テーブル**（辞書）

  * `start` / `end`: **UTC の datetime**
  * `checked_in`: 到着済み？
  * `auto_released`: ノーショーで自動解放された？
  * `closed`: 終了ウィンドウで occupancy=0 になってクローズした？
  * `overstayed`: 終了後も occupancy>0 ＝超過利用？

* 予約作成API `/mock/reservations/create` や、シナリオAPI `/simulate/reservation_scenario` などからこの `_rsv_upsert` が呼ばれます。

* 部屋IDは `_RSV_STATE[rid]["room_id"]` に追加で格納。

### ノーショー監視ループ `no_show_watcher_loop`

```python
def no_show_watcher_loop():
    while True:
        now = datetime.now(timezone.utc)
        with _RS_LOCK:
            items = list(_RSV_STATE.items())
        for rid, st in items:
            if st["checked_in"] or st["auto_released"]:
                continue
            window_end = st["start"] + timedelta(minutes=ARRIVAL_WINDOW_AFTER_MIN)
            deadline   = window_end + timedelta(minutes=NO_SHOW_GRACE_MIN)
            if now >= deadline:
                st["auto_released"] = True
                # KPIログ: no_show_detected
                # 予約API / 鍵API に auto-release / revoke を投げる（あれば）
        time.sleep(30)
```

* **30秒ごと**に全予約を走査し、

  * **到着許容窓＋猶予**を過ぎても `checked_in` になっていない予約を `auto_released=True` に。
  * KPIログに `event: "no_show_detected"` を書く。
  * 外部APIが設定されていれば、予約システムに auto-release, 鍵システムに revoke を投げる。

### 終了監視ループ `end_watcher_loop`

```python
def end_watcher_loop():
    while True:
        now_utc = datetime.now(timezone.utc)
        with _RS_LOCK:
            items = list(_RSV_STATE.items())
        for rid, st in items:
            if st["auto_released"]:
                continue

            room = st.get("room_id", ROOM_ID)
            close_from = st["end"] - timedelta(minutes=END_CLOSE_WINDOW_MIN)
            close_to   = st["end"] + timedelta(minutes=END_CLOSE_WINDOW_MIN)

            # 終了ウィンドウ内で occupancy=0 → reservation_closed
            # 終了＋OVERSTAY_GRACE_MIN 超え＆ occupancy>0 → overstay_detected
        time.sleep(30)
```

* 予約終了前後で occupancy=0 なら「自然に終わった」→ `reservation_closed`
* 終了後＋猶予超えで occupancy>0 なら「超過利用」→ `overstay_detected`
  （外部決済APIがあればここから課金フローへつなげるイメージ）

---

## 6. `LineCounter`：AIカメラのバウンディングボックス → enter/exit

```python
class LineCounter:
    def __init__(..., axis="y", pos=360, dir_positive_is_enter=True, ...):
        self.tracks = {}
        self._next_id = 1

    def update(self, det: dict):
        # det: {"1": {"X","Y","x","y","P"}, "2":...}
        # 1) 信頼度とサイズでフィルタ
        # 2) 既存トラックに IoU でマッチング
        # 3) center の前フレーム・今フレームの位置関係から
        #    ライン crossing を検出、enter/exit を決定
        # 4) crossing があったら events に {"type": "enter"/"exit", "confidence": P} で追加
        # 5) 新しい物体は新規トラックとして登録
        return events
```

* `update(det)` に **1フレーム分の検出結果（bbox群）**を渡すと、

  * 内部トラッキングを更新しつつ、
  * 「今回のフレームでラインを跨いだ物体」を `enter` / `exit` のイベントとして返します。
* `axis` と `dir_positive_is_enter` に基づいて、「どちら向きを入室とするか」を決めています。

これが **実機カメラからの推論結果** or **シミュレーションAPIから送った仮想 bbox** に対して共通で使われます。

---

## 7. `process_event`：enter/exit → occupancy / 予約ヒット / KPI

```python
def process_event(room_id: str, ev_type: str, ts_iso: str, count_delta: int, confidence: float):
    global _OCCUPANCY
    delta = 1 if ev_type == "enter" else (-1 if ev_type == "exit" else 0)

    # 信頼度不足はfiltered扱い
    if confidence < CONFIDENCE_MIN:
        _METRICS["filtered"]++
        KPIログ(event="filtered", confidence, occupancy)
        return

    # 在室人数とカウンタ更新
    with _O_LOCK:
        _OCCUPANCY = max(0, _OCCUPANCY + delta)
        occ_after = _OCCUPANCY
        if ev_type in ("enter","exit"):
            _METRICS[ev_type] += 1

    matched = None
    if ev_type == "enter":
        # 到着時 → 予約ヒットを試みる
        rid = checkin_attempt(room_id, ts_iso)
        matched = bool(rid)
        with _O_LOCK:
            if matched: _METRICS["checkin_success"] += 1
            else:       _METRICS["checkin_fail"]    += 1
    else:
        # exit は今のところ occupancy 変化のみ (将来ここで「予約終了条件」への寄与もありうる)
        log_event("edge_event", ...)

    # KPIログ書き込み (enter/exit, occupancy, matched, reservation_id, 各種パラメータ)
```

* **カメラ由来イベントもシミュレーション由来イベントもすべてここで処理**されます。
* enter 時だけ `checkin_attempt` を呼び、予約ヒットを試すようにしています。

---

## 8. 予約ヒット処理 `checkin_attempt`

```python
def checkin_attempt(room_id: str, ts_iso: str):
    if AB_BUCKET.startswith("control"):
        # control バケットなら「予約連携なし」シナリオ
        return None

    if RESERVATION_API_BASE:
        # 外部予約APIが設定されている場合 → そちらに POST して判定を委譲
        # matched/rid を受け取って _record_checkin / grant_lock
    else:
        # 外部予約APIがない場合 → モック機構で内部の _RSV_STATE からヒットを探す
        now_dt = datetime.fromisoformat(ts_iso)  # JST → naive local
        with _RS_LOCK:
            for r_id, st in _RSV_STATE.items():
                w_start = st["start"] - ARRIVAL_WINDOW_BEFORE_MIN
                w_end   = st["end"]   + NO_SHOW_GRACE_MIN
                if w_start <= now_dt <= w_end:
                    rid = r_id
                    break
        if rid:
            _record_checkin(rid); grant_lock(room_id); return rid
        else:
            return None
```

* **A/B テスト**：

  * `treatment`: 到着時に予約マッチを試みる
  * `control`  : 予約連携をスキップ（KPI比較用）

* 現状あなたの環境では、**外部予約APIは使わず、モックの内部予約表 `_RSV_STATE` を照会**しています。

---

## 9. シミュレーション API 群

### `/simulate/visit`：1人の来訪（入→滞在→出）

```python
@app.post("/simulate/visit")
def simulate_visit():
    p = request.get_json() or {}
    target_room = p.get("room_id", ROOM_ID)
    before = _snapshot_metrics()
    events, steps, dwell_ms = _simulate_visit_once(p, target_room)
    after = _snapshot_metrics()
    summary = {..., "metrics_delta": _metrics_delta(after,before)}
    return jsonify(...)
```

* `dwell_ms` (滞在時間) を指定すると、

  * `LineCounter` に対して「ラインをまたいで入る → 少し止まる → 逆方向にまたいで出る」という bbox 軌跡を流します。
* **enter/exit のイベントは全て `process_event(...)` に入り、実際の予約ヒット／occupancy 変化が起こる**ので、本番と同じ挙動を「カメラなしで再現」できます。
* `/ui/dev` の「1人来訪(Visit)」ボタンがこれを叩いています。

### `/simulate/scenario`：複数人ランダム来訪

```python
@app.post("/simulate/scenario")
def simulate_scenario():
    visitors = ...  # デフォルト3
    for i in range(visitors):
        vp = dict(p)
        vp["dwell_ms"] = random.randint(min_d, max_d)
        events, steps, used_dwell = _simulate_visit_once(vp, target_room)
        ...
```

* **複数人がバラバラに入って出ていく**シナリオを生成。
* `/ui/dev` の「5人ランダム」「treatment/control」ボタンから呼ばれます。

  * `runMulti` / `runMultiForBucket` で `bucket` を切り替えたうえで `simulate/scenario` を叩きます。

### `/simulate/reservation_scenario`：予約＋ノーショー＋来訪シナリオ

```python
@app.post("/simulate/reservation_scenario")
def simulate_reservation_scenario():
    pattern: "no_show" / "mixed" / "all_show"
    num_reservations
    id_prefix
    room_id
    show_ratio (mixed用)
```

* **予約自体を自動生成して、その後の来訪有無も含めてまとめてシミュレート**する API。
* パターンごと：

  * `"no_show"` : 全予約がノーショー（`no_show_detected` ログのみ）
  * `"mixed"`   : `show_ratio` の確率で来訪する／しない
  * `"all_show"`: 全予約が来訪する（enterイベントを自動発行）
* ここから発行される enter イベントは **実際に `process_event → checkin_attempt → 予約ヒット` の流れに入る**ので、ノーショー/来訪パターンの KPI を簡単に生成できます。
* `/ui/dev` の **「予約＋来訪シナリオ」カード** がこれを叩きます。

---

## 10. モック予約・鍵API

### `/mock/reservations/create`

* **手動で GUI から予約を作る**ためのエンドポイント。
* パラメータ：

  * `reservation_id`, `room_id`, `start_in_min`, `duration_min`
* 予約時間が過去にならないようチェックし、**同じ部屋の既存予約と時間帯が重ならないか**を検査。
* `_RSV_STATE` に `start/end/room_id` などを格納。

UI では `/ui/dev` の「予約作成」フォームから利用しています。

### `/mock/reservations/upcoming` & `/mock/reservations/all`

* `upcoming?room_id=...`：その部屋の予約一覧（タイムライン用／生JSON表示用）
* `all`: 全予約を部屋横断で一覧化（/ui/devの下部テーブル）

### `/mock/reservations/checkin-attempt`

* **本番の予約APIのモック版**。`checkin_attempt` が外部APIではなくこのエンドポイントを叩くイメージです。
* 今は主に debug 用（CLI や curl から直接叩ける）。

### `/reservations/extend`

* **延長処理のモック**。
* `reservation_id` と `extend_min` を受け取り、

  * まず在室人数 `_OCCUPANCY > 0` を確認（誰もいないのに延長はNG）。
  * `_RSV_STATE[rid]["end"]` を延長し、`reservation_extended` を KPI ログに書きます。
* `/ui/dev` の「延長操作」から呼ばれます。

### `/mock/rooms/<room_id>/grant`, `/mock/locks/revoke`

* 鍵システムがない環境で、「鍵付与・鍵解除が成功したことにする」モックエンドポイント。

---

## 11. KPI とメトリクス API

* `/metrics`: 現在の在室・カウンタ・バケット
* `/kpi/summary`: A/B バケットごとの

  * enter 回数
  * そのうち予約ヒットした数（`success`）
  * 成功率 `rate = success/enter`
* `/kpi/qr_summary`: `event=="qr_checkin"` だけ集計

  * QR チェックイン全体数 / 成功数 / 成功率
* `/kpi/no_show_summary`: `no_show_detected` の件数集計
* `/kpi/tail`: ログ末尾 N 件を JSON で返す
* `/kpi/reset_log`: ログファイルを空にする
* `/metrics/reset`: 在室人数とメトリクスをゼロクリア

これらは `/ui`, `/ui/dev` のダッシュボード・KPI サマリ・ログパネルに使われています。

---

## 12. UI エンドポイント

### `/ui`：日本語ダッシュボード

* 在室人数 / A/B バケット / 入室・退室 / QR成功・失敗 のカード
* 以下を定期的にFetch

  * `/metrics`
  * `/kpi/summary`
  * `/kpi/qr_summary`
  * `/kpi/no_show_summary`

### `/ui/qr`：手動QRチェックイン

* 予約IDを手入力して `/qr/checkin` を叩くだけの超シンプル画面。

### `/ui/dev`：開発者向けツール

**大きく 4 ブロックあります：**

1. **モニタカード（metrics表示）**

   * `/metrics` の値をほぼリアルタイムで表示。

2. **「部屋予約状況 (Timeline)」**

   * 上のプルダウンで部屋選択 (`R-0001`〜`R-0004`)
   * `loadUpcoming()` → `/mock/reservations/upcoming?room_id=...`
   * 30分前〜60分後の 90分ウィンドウ上に予約バーを描画
   * `checked_in` → 青バー、`auto_released` → 赤バー などで状態を可視化
   * 「全室の予約一覧」テーブルは `/mock/reservations/all` を使って全体俯瞰

3. **シミュレーション操作**

   * **カメラ検知シミュレーション**

     * 「1人来訪」 → `/simulate/visit`
     * 「5人ランダム」 → `/simulate/scenario`
     * treatment/control ボタンで A/B バケットを切り替えてから `runMulti`
   * **予約＋来訪シナリオ**

     * `/simulate/reservation_scenario` を叩き、ノーショーのみ／混在／全員来訪の KPI をまとめて生成
     * 結果は読みやすいテキスト（pattern, room, show/no_show 数, Δenter, Δcheckin_hit, Δocc）で表示

4. **管理・設定**

   * `line_axis`, `line_pos`, `min_score`, `confidence_min`, etc → `/config/update`
   * 到着窓＋ノーショー猶予 → `/config/update` 経由で書き換え
   * Bucket 切り替え → `/ab/set`
   * カウンタとKPIログの Reset
   * `/kpi/tail` ログ末尾プレビュー

---

## 13. バックグラウンドスレッドと起動

```python
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
```

* `no_show_watcher_loop`, `end_watcher_loop`, `edge_poller_loop` が**開発サーバの「再起動子プロセス」側だけで起動**するようにガード (`WERKZEUG_RUN_MAIN`)。
* `edge_poller_loop` は `AiCameraRepository.fetch_inference_result()` を定期呼び出しして、実機カメラとつながっている場合の enter/exit を自動処理します。
* `_auto_open_ui` は、ローカル/ Codespaces / Gitpod 向けに「最もそれっぽい `/ui` のURL」を推定してブラウザで開きます。

---

## まとめ：今のコードの立ち位置

* **実運用用コア機能**

  * カメラ→ライン通過→enter/exit 判定 (`LineCounter`)
  * 在室人数カウンタ + 予約ヒット→鍵付与 (`process_event`, `checkin_attempt`, `grant_lock`)
  * ノーショー自動解放 & 終了/オーバーステイ検出 (`no_show_watcher_loop`, `end_watcher_loop`)
  * KPI ログ（JSONL）→ APIで集計 (`/kpi/...`)

* **開発/検証用機能 (GUI中心)**

  * `/ui`：運営が見る簡易ダッシュボード
  * `/ui/qr`：自己申告ルート（QR）
  * `/ui/dev`：予約生成＋ライン通過シミュレーション＋予約&ノーショーシナリオ
  * `/mock/...` & `/simulate/...`: すべてカメラ無し・外部API無しで「本番にかなり近い状態」を再現するためのモック/シナリオAPI

---

もし、

* 「この関数のここの if 分岐の意図をもっと知りたい」
* 「ノーショー判定の時刻計算を図で説明してほしい」
* 「/ui/dev のこのボタンを押した時にどの API がどんな順で呼ばれるかをフローチャートで見たい」

みたいなピンポイントの深堀りがあれば、そこをさらに分解して説明します！
