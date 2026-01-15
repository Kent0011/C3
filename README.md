# Room Reservation System

会議室予約および利用状況監視システムです。Webインターフェースからの予約管理と、カメラ（またはダミーデータ）による入退室検知を組み合わせ、不正利用（無断キャンセル、時間超過など）に対するペナルティ管理機能を提供します。

## クイックスタート

### ローカル実行

```bash
$ python main.py
```

### Docker実行

```bash
# バックグラウンドで実行
docker-compose up -d --build
```

### アクセス

アプリケーションは `http://localhost:8000` でアクセス可能です。

## ドキュメント

- [UI 操作ガイド（日本語）](ui_guide_ja.md)

## 環境変数

`.env` ファイル等で以下の設定が可能です。

- `CONSOLE_ENDPOINT`: AIカメラコンソールエンドポイントのURL
- `AUTH_ENDPOINT`: 認証エンドポイントのURL
- `CLIENT_ID`: クライアントID
- `CLIENT_SECRET`: クライアントシークレット
- `DEVICE_ID`: デバイスID
- `OCCUPANCY_MODE`: `camera` (実機) または `dummy` (シミュレーション)
- `USE_SQLITE`: `true` で永続化有効 (デフォルト: false)

---

## アーキテクチャ

本システムは、FlaskベースのWebアプリケーションと、バックグラウンドで動作する監視タスクから構成されています。
Repositoryパターンを採用しており、データアクセス層（インメモリ/SQLite）とビジネスロジックが分離されています。

```mermaid
graph TD
    User((User))
    Admin((Admin))
    
    subgraph "Room Reservation System"
        WebUI["Web UI / Frontend<br/>(Templates & Static)"]
        API["Flask API Server<br/>(main.py)"]
        BgTask["Background Monitoring Task<br/>(Thread)"]
        
        subgraph "Services"
            RSM[RoomStateManager]
            PS[PenaltyService]
            OP[OccupancyProvider]
        end
        
        subgraph "Repositories"
            ResRepo[ReservationRepository]
            PenRepo[PenaltyRepository]
            UserRepo[UserRepository]
            CamRepo[AiCameraRepository]
        end
        
        DB[("SQLite DB")]
        ExtCam[External AI Camera System]
    end

    User --> WebUI
    Admin --> WebUI
    WebUI --> API
    
    API --> RSM
    API --> PS
    API --> ResRepo
    API --> UserRepo
    
    BgTask --> OP
    BgTask --> RSM
    
    RSM --> ResRepo
    RSM --> PS
    
    PS --> PenRepo
    
    ResRepo --> DB
    PenRepo --> DB
    UserRepo --> DB
    
    OP --> CamRepo
    CamRepo --> ExtCam
```

### 主要コンポーネント

1.  **Web Application (`main.py`)**
    *   ユーザー向けの画面提供 (`/`, `/app/ui`)
    *   予約管理、状態取得のためのREST API (`/api/*`)
    *   デバッグ用インターフェース

2.  **Background Monitoring Task**
    *   5秒間隔で部屋の占有状態をポーリング
    *   予約情報と実際の占有状態を比較し、状態遷移（空室→利用中など）を管理
    *   違反（無断キャンセル、時間超過、未予約利用）検知時にペナルティを発行

3.  **Repositories**
    *   データの永続化を担当。開発用(`InMemory`)と本番用(`Sqlite`)の実装が存在。

## データ構造 (E-R図)

システムの主要なデータモデルです。SQLiteデータベース `data/room_reservation.db` に保存されます。

```mermaid
erDiagram
    users {
        string user_id PK "ユーザーID"
        string password_hash "ハッシュ化パスワード"
    }
    
    reservations {
        string reservation_id PK "予約ID"
        string room_id "部屋ID"
        string user_id FK "予約者"
        string start_time "開始日時(ISO8601)"
        string end_time "終了日時(ISO8601)"
        string status "active/cancelled"
        string created_at
        string updated_at
    }
    
    penalty_events {
        integer id PK
        string user_id FK "対象ユーザー"
        string reason "ペナルティ理由"
        integer points "付与ポイント"
        string timestamp "発生日時"
    }
    
    user_bans {
        string user_id PK "対象ユーザー"
        string ban_until "BAN解除日時"
    }
    
    users ||--o{ reservations : "makes"
    users ||--o{ penalty_events : "receives"
    users ||--o| user_bans : "has"
```

## シーケンス図

### 1. 予約作成フロー

ユーザーが予約を行う際の処理フローです。ペナルティによるBAN状態のチェックが行われます。

```mermaid
sequenceDiagram
    actor User
    participant WebUI
    participant API
    participant PenaltyService
    participant ReservationRepo
    participant DB

    User->>WebUI: 予約情報入力 & 送信
    WebUI->>API: POST /api/reservations
    
    activate API
    API->>PenaltyService: get_summary(user_id)
    PenaltyService-->>API: BAN状態, 累積ポイント
    
    alt ユーザーがBANされている
        API-->>WebUI: 403 Forbidden (BAN中)
        WebUI-->>User: エラー表示「予約できません」
    else 正常 (BANされていない)
        API->>ReservationRepo: create_reservation(...)
        activate ReservationRepo
        ReservationRepo->>DB: 重複チェック & INSERT
        DB-->>ReservationRepo: Success
        ReservationRepo-->>API: Reservation Object
        deactivate ReservationRepo
        API-->>WebUI: 201 Created
        WebUI-->>User: 完了メッセージ & 一覧更新
    end
    deactivate API
```

### 2. 監視・状態管理フロー

バックグラウンドで常時実行され、予約と実際の利用状況の整合性をチェックします。

```mermaid
sequenceDiagram
    participant BgTask as Monitoring Task
    participant OP as OccupancyProvider
    participant RSM as RoomStateManager
    participant Repo as Repositories
    participant PS as PenaltyService

    loop Every 5 seconds
        BgTask->>OP: get_is_occupied(now)
        OP-->>BgTask: is_occupied (True/False)
        
        BgTask->>RSM: update_state(is_occupied, now)
        activate RSM
        
        RSM->>Repo: 現在時刻の予約を取得
        Repo-->>RSM: Reservation List
        
        Note right of RSM: 状態遷移ロジック実行<br/>(入室/退室/不在判定)
        
        opt 違反検知 (No-Show, Overstay等)
            RSM->>PS: add_penalty(user_id, reason, points)
            PS->>Repo: ペナルティ保存 & BAN判定
        end
        
        RSM-->>BgTask: 最新の状態情報 (Alerts, Status)
        deactivate RSM
        
        BgTask->>BgTask: Update Global System Status
    end
```

## エコシステム

- **Backend**: Python (Flask)
- **Database**: SQLite3 (プロダクション想定), In-Memory (開発用)
- **AI Integration**: 外部のAIカメラAPIと連携し、人数カウントデータを取得 (`OccupancyProvider`)
- **Container**: Docker, Docker Compose
