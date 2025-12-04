from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

# 仮想時計の内部状態
_use_simulated: bool = False  # True のとき仮想時計を使う
_scale: float = 1.0  # 1.0: 等速, 10.0: 10倍速 など
_base_real: datetime | None = None  # R₀ (JST)
_base_sim: datetime | None = None  # S₀ (JST)


def now_jst() -> datetime:
    """
    システム内で使用する「現在時刻」。
    通常は実時間JST、デバッグ時はスケーリング＋オフセットを適用した仮想時刻を返す。
    """
    real_now = datetime.now(tz=JST)

    if not _use_simulated or _base_real is None or _base_sim is None:
        return real_now

    # 実時間の経過
    delta_real = real_now - _base_real

    # スケールをかけた経過時間
    scaled = delta_real * _scale  # timedelta × float は Python でOK

    return _base_sim + scaled


def to_jst(dt: datetime) -> datetime:
    """
    任意の datetime を JST に正規化する。
    - naive (tzinfo=None) の場合: JSTとして解釈して tzinfo=JST を付与
    - tz-aware の場合: JST に変換して返す
    """
    if dt.tzinfo is None:
        # 「これはJSTである」と仮定して tzinfo を付与
        return dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def parse_jst_datetime(s: str) -> datetime:
    """
    ISO8601 っぽい文字列から datetime を生成し、JST tz-aware に正規化する。

    - タイムゾーン付き（+09:00 等）の場合: そのオフセットを尊重したうえで JST に変換
    - タイムゾーンなしの場合        : 「それは JST で書かれている」とみなして tzinfo=JST を付ける
    """
    dt = datetime.fromisoformat(s)
    return to_jst(dt)


def format_jst_iso(dt: datetime) -> str:
    """
    datetime を JST に統一した上で ISO8601 文字列 (+09:00付き) にする。
    APIレスポンスやログ出力用。
    """
    dt_jst = to_jst(dt)
    return dt_jst.isoformat()


def set_simulated_time(
    sim_now: datetime,
    scale: float = 1.0,
) -> None:
    """
    sim_now を「現時点での仮想的な現在時刻」として設定し、
    そこから scale 倍速で進むようにする。
    """
    global _use_simulated, _scale, _base_real, _base_sim

    if scale <= 0:
        raise ValueError("scale must be positive")

    real_now = datetime.now(tz=JST)
    _use_simulated = True
    _scale = scale
    _base_real = real_now
    _base_sim = to_jst(sim_now)


def clear_simulated_time() -> None:
    global _use_simulated, _scale, _base_real, _base_sim
    _use_simulated = False
    _scale = 1.0
    _base_real = None
    _base_sim = None


def get_time_status() -> dict:
    real_now = datetime.now(tz=JST)
    cur = now_jst()
    return {
        "use_simulated": _use_simulated,
        "scale": _scale,
        "system_now": real_now.isoformat(),
        "current_now": cur.isoformat(),
        "base_real": _base_real.isoformat() if _base_real else None,
        "base_sim": _base_sim.isoformat() if _base_sim else None,
    }
