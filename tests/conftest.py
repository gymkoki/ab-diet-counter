"""テスト共通のヘルパー。

体重を1週間以上記録していない人は食事の解析ができない（オーナー指示 2026-09）。
そのため「解析そのもの」を試すテストは、この入口で弾かれないように
体重を1件入れてから叩く必要がある。その準備をここにまとめる。
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as m  # noqa: E402


def seed_weight(uid, days_ago=0, weight=70.0):
    """uid の体重記録を days_ago 日前の日付で1件入れる。入れた日付を返す。"""
    m.init_db()
    date = (datetime.datetime.now(m.JST).date()
            - datetime.timedelta(days=days_ago)).isoformat()
    ts = datetime.datetime.now(m.JST).isoformat()
    with m._db_lock:
        conn = m._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM daily_weight WHERE user_id={m.PH} AND date={m.PH}",
                        (uid, date))
            cur.execute(
                f"INSERT INTO daily_weight (user_id, date, weight, created_at) "
                f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})",
                (uid, date, weight, ts))
            conn.commit()
        finally:
            conn.close()
    return date


def clear_weight(uid):
    """uid の体重記録を全部消す（＝解析が止まる状態にする）。"""
    m.init_db()
    with m._db_lock:
        conn = m._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM daily_weight WHERE user_id={m.PH}", (uid,))
            conn.commit()
        finally:
            conn.close()


# ── 会員限定ゲート（2026-09-15〜）─────────────────────────────
# 解析系のテストは「合言葉」を通っていない端末として実行されるため、
# 開始日を過ぎるとゲートで弾かれて一斉に落ちてしまう。
# 既定ではゲートを無効（開始日をはるか未来に）にしておき、
# ゲート自体を試すテストだけが明示的に有効化する。
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _member_gate_disabled_by_default(monkeypatch):
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2099-01-01", raising=False)


def unlock_device(uid, token="test-token"):
    """uid の端末を「合言葉を通過済み」にする。"""
    m.init_db()
    ts = datetime.datetime.now(m.JST).isoformat()
    with m._db_lock:
        conn = m._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM member_devices WHERE user_id={m.PH}", (uid,))
            cur.execute(
                f"INSERT INTO member_devices (user_id, token, activated_at) "
                f"VALUES ({m.PH},{m.PH},{m.PH})", (uid, token, ts))
            conn.commit()
        finally:
            conn.close()
    return token


def clear_devices():
    """認証済み端末を全部消す。"""
    m.init_db()
    with m._db_lock:
        conn = m._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM member_devices")
            conn.commit()
        finally:
            conn.close()
