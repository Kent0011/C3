import os
import sqlite3
from pathlib import Path

# プロジェクトルートを自動推定
# db.py は src/Repository/db.py にあるので、parents[2] がプロジェクトルート（/workspaces/C3 想定）
BASE_DIR = Path(__file__).resolve().parents[2]

# デフォルトのDBパスは「プロジェクト直下の data/room_reservation.db」
DEFAULT_DB_PATH = BASE_DIR / "data" / "room_reservation.db"


def get_db_path() -> str:
    """
    実際に使うDBパスを返す。

    - 環境変数 DB_PATH があればそれを優先
    - なければプロジェクト直下 data/room_reservation.db を使う
    """
    env_path = os.getenv("DB_PATH")
    if env_path:
        return env_path
    return str(DEFAULT_DB_PATH)


def ensure_db_dir():
    """
    DBファイルを置くディレクトリを作成する。
    """
    db_path = Path(get_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)


def get_connection():
    ensure_db_dir()
    return sqlite3.connect(get_db_path(), check_same_thread=False)


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # --- users テーブル（新規） ---
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS users (
      user_id       TEXT PRIMARY KEY,
      password_hash TEXT NOT NULL
    );
    """
    )

    # --- reservations テーブル（既存） ---
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS reservations (
      reservation_id TEXT PRIMARY KEY,
      room_id        TEXT NOT NULL,
      user_id        TEXT NOT NULL,
      start_time     TEXT NOT NULL, -- ISO8601 (JST)
      end_time       TEXT NOT NULL,
      status         TEXT NOT NULL,
      created_at     TEXT NOT NULL,
      updated_at     TEXT NOT NULL
    );
    """
    )
    cur.execute(
        """
    CREATE INDEX IF NOT EXISTS idx_res_room_time
      ON reservations(room_id, start_time, end_time);
    """
    )
    cur.execute(
        """
    CREATE INDEX IF NOT EXISTS idx_res_user_time
      ON reservations(user_id, start_time);
    """
    )

    # --- penalty_events テーブル（新規） ---
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS penalty_events (
      id        INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id   TEXT NOT NULL,
      reason    TEXT NOT NULL,
      points    INTEGER NOT NULL,
      timestamp TEXT NOT NULL  -- ISO8601 (JST)
    );
    """
    )
    cur.execute(
        """
    CREATE INDEX IF NOT EXISTS idx_penalty_user_time
      ON penalty_events(user_id, timestamp);
    """
    )

    # --- user_bans テーブル（新規） ---
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS user_bans (
      user_id   TEXT PRIMARY KEY,
      ban_until TEXT NOT NULL   -- ISO8601 (JST)
    );
    """
    )

    conn.commit()
    conn.close()
