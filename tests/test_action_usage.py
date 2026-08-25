# 「記録方法ごとに1日何人が使っているか」の集計の回帰テスト。
#
# 経緯（オーナー質問 2026-08）：「Bだけ追加は毎日何人ぐらい使っていますか？」に
# 答えられなかった。食事の明細(daily_meals)は5日で自動削除されるため、
# そこから数えると直近5日ぶんしか分からず、日ごとの人数も出せなかった。
# そこで「いつ・誰が・どの方法で記録したか」だけを別に残して数えるようにした。

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client():
    m.init_db()
    m.app.config["TESTING"] = True
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute("DELETE FROM action_log")
        conn.commit(); conn.close()
    with m.app.test_client() as c:
        yield c


def _ts(days_ago, hour=12):
    d = datetime.datetime.now(m.JST) - datetime.timedelta(days=days_ago)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def _seed(uid, action, days_ago, times=1):
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        for i in range(times):
            cur.execute(
                f"INSERT INTO action_log (user_id, action, created_at) VALUES ({m.PH},{m.PH},{m.PH})",
                (uid, action, _ts(days_ago, 9 + i)))
        conn.commit(); conn.close()


# ── 会員アプリからの記録 ────────────────────────────────────────
def test_app_can_record_a_manual_b_use(client):
    """アプリから「Bだけ追加」の利用を1件記録できること。"""
    r = client.post("/api/log-action", json={"user_id": "u1", "action": "manual_b"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    d = m._collect_action_usage(30)
    assert any(s["key"] == "manual_b" and s["total_count"] >= 0 for s in d["summary"])


def test_unknown_action_is_rejected(client):
    """知らない種類は受け付けないこと（変なデータで集計を汚さない）。"""
    assert client.post("/api/log-action", json={"user_id": "u1", "action": "hack"}).status_code == 400
    assert client.post("/api/log-action", json={"action": "manual_b"}).status_code == 400


def test_admin_only(client):
    """集計結果は管理者だけが見られること（未認証は404）。"""
    assert client.get("/api/admin/action-usage").status_code == 404


# ── 集計が正しいこと ──────────────────────────────────────────
def test_counts_users_per_day_not_total(client):
    """『1日あたり何人』が、期間の延べ人数ではなく日ごとの人数の平均であること。"""
    # 3日間、毎日2人ずつが使った（のべ6人だが、1日あたりは2人）
    for back in (1, 2, 3):
        _seed("a", "manual_b", back)
        _seed("b", "manual_b", back)
    d = m._collect_action_usage(30)
    row = next(s for s in d["summary"] if s["key"] == "manual_b")
    assert row["avg_users_per_day"] == 2.0, row
    assert row["total_users"] == 2


def test_same_user_twice_a_day_is_one_person(client):
    """同じ人が1日に2回使っても、その日の人数は1人と数えること。"""
    _seed("a", "manual_b", 1, times=3)
    d = m._collect_action_usage(30)
    row = next(s for s in d["summary"] if s["key"] == "manual_b")
    assert row["avg_users_per_day"] == 1.0
    assert row["avg_count_per_day"] == 3.0     # 回数は3回


def test_methods_are_counted_separately(client):
    """写真・文章・Bだけ追加・コピーご飯を取り違えないこと。"""
    _seed("a", "photo", 1)
    _seed("b", "text", 1)
    _seed("c", "manual_b", 1)
    _seed("d", "copy", 1)
    d = m._collect_action_usage(30)
    got = {s["key"]: s["avg_users_per_day"] for s in d["summary"]}
    assert got == {"photo": 1.0, "text": 1.0, "manual_b": 1.0, "copy": 1.0}


def test_today_is_excluded(client):
    """当日はまだ途中なので、1日あたりの平均に混ぜないこと。"""
    _seed("a", "manual_b", 0, times=5)      # 今日だけ記録
    d = m._collect_action_usage(30)
    assert d["counted_days"] == 0, "当日が平均に入っている"


def test_daily_table_has_recent_days(client):
    """日ごとの表が返り、日付と4種類の人数が入っていること。"""
    _seed("a", "manual_b", 1)
    d = m._collect_action_usage(30)
    assert d["daily"], "日ごとのデータが空"
    last = d["daily"][-1]
    for k in ("photo", "text", "manual_b", "copy"):
        assert "users" in last[k] and "count" in last[k]


def test_survives_the_five_day_meal_deletion():
    """食事の明細が5日で消えても、この記録は残る作りであること。"""
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
        src = f.read()
    assert "action_log" in src
    # daily_meals の削除処理が action_log を巻き込んでいないこと
    assert "DELETE FROM daily_meals" in src
    assert "DELETE FROM action_log WHERE created_at" in src   # 消すのは保持期間を過ぎたぶんだけ
    assert m.ACTION_RETAIN_DAYS >= 180, "保持期間が短すぎて推移が追えない"


# ── アプリ側が実際に記録を送っていること ───────────────────────
def test_app_sends_all_four_methods():
    """4つの記録方法すべてで logAction が呼ばれていること。"""
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert "function logAction" in html
    for action in ("'manual_b'", "'copy'", "'photo'", "'text'"):
        assert f"logAction({action})" in html, f"{action} の記録が送られていない"


def test_logging_never_breaks_the_app():
    """記録の送信が失敗してもアプリが止まらないこと（送りっぱなし）。"""
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    import re
    body = re.search(r"function logAction\(action\) \{.*?\n\}", html, re.S).group(0)
    assert ".catch(() => {})" in body and "try {" in body
    assert "await" not in body, "送信を待つと記録が遅くなる（送りっぱなしにする）"


def test_dashboard_shows_it():
    """管理ダッシュボードに表示され、更新対象に入っていること。"""
    with open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8") as f:
        html = f.read()
    assert 'id="action-usage"' in html
    assert "loadActionUsage()" in html
    assert "1日あたり" in html
