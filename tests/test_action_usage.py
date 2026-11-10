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
def test_app_can_record_a_photo_use(client):
    """アプリから記録方法の利用を1件記録でき、集計に出ること。"""
    r = client.post("/api/log-action", json={"user_id": "u1", "action": "photo"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    d = m._collect_action_usage(30)
    assert any(s["key"] == "photo" and s["total_count"] == 1 for s in d["summary"])


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
        _seed("a", "photo", back)
        _seed("b", "photo", back)
    d = m._collect_action_usage(30)
    row = next(s for s in d["summary"] if s["key"] == "photo")
    assert row["avg_users_per_day"] == 2.0, row
    assert row["total_users"] == 2


def test_same_user_twice_a_day_is_one_person(client):
    """同じ人が1日に2回使っても、その日の人数は1人と数えること。"""
    _seed("a", "photo", 1, times=3)
    d = m._collect_action_usage(30)
    row = next(s for s in d["summary"] if s["key"] == "photo")
    assert row["avg_users_per_day"] == 1.0
    assert row["avg_count_per_day"] == 3.0     # 回数は3回


def test_methods_are_counted_separately(client):
    """写真・文章・コピーご飯を取り違えないこと。"""
    _seed("a", "photo", 1)
    _seed("b", "text", 1)
    _seed("d", "copy", 1)
    _seed("d", "copy", 2)
    d = m._collect_action_usage(30)
    got = {s["key"]: s["total_count"] for s in d["summary"]}
    assert got == {"photo": 1, "text": 1, "copy": 2}


def test_today_is_excluded(client):
    """当日はまだ途中なので、1日あたりの平均に混ぜないこと。"""
    _seed("a", "photo", 0, times=5)      # 今日だけ記録
    d = m._collect_action_usage(30)
    assert d["counted_days"] == 0, "当日が平均に入っている"


def test_daily_table_has_recent_days(client):
    """日ごとの表が返り、日付と3種類の人数が入っていること。"""
    _seed("a", "photo", 1)
    d = m._collect_action_usage(30)
    assert d["daily"], "日ごとのデータが空"
    last = d["daily"][-1]
    for k in ("photo", "text", "copy"):
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
def test_app_sends_all_current_methods():
    """現在ある記録方法すべてで logAction が呼ばれていること。

    ※「Bだけ追加」(manual_b) はオーナー指示 2026-08 で廃止したため、
      アプリからはもう送られない。ただし過去の集計は残すので、
      サーバー側では引き続き受け付ける（下のテストで固定）。"""
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert "function logAction" in html
    for action in ("'copy'", "'photo'", "'text'"):
        assert f"logAction({action})" in html, f"{action} の記録が送られていない"
    assert "logAction('manual_b')" not in html, "廃止した「Bだけ追加」がまだ送られている"


def test_manual_b_stays_valid_for_past_records(client):
    """廃止後も manual_b は集計対象として受け付けること（過去のデータを守る）。"""
    r = client.post("/api/log-action", json={"user_id": "u1", "action": "manual_b"})
    assert r.status_code == 200, "過去データの集計に必要なので弾いてはいけない"


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


# ── 廃止した「Bだけ追加」はダッシュボードに出さない（オーナー指示 2026-11-10） ──
# 「🍙 Bを手動追加 が使われていない」と出て、今ある「文章で手動入力」のことと紛らわしかった。
def test_retired_manual_b_is_hidden_from_action_usage(client):
    """過去の manual_b の記録があっても、記録方法の集計（平均・日ごとの表）に出ないこと。"""
    _seed("a", "manual_b", 1, times=2)
    _seed("b", "photo", 1)
    d = m._collect_action_usage(30)
    assert {s["key"] for s in d["summary"]} == {"photo", "text", "copy"}
    assert all("manual_b" not in row for row in d["daily"])


def test_retired_manual_b_is_hidden_from_feature_usage(client):
    """機能別の利用状況（AIの改善提案にも渡る）の記録方法の内訳にも出さないこと。"""
    import json
    today = datetime.datetime.now(m.JST).strftime("%Y-%m-%d")
    payload = {"food": {"items": [
        {"id": 1, "isOil": True, "result": {"total_b_count": 1}},
        {"id": 2, "isText": True, "result": {"total_b_count": 1}},
    ]}}
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute(f"DELETE FROM daily_meals WHERE user_id={m.PH}", ("fu-oil",))
        cur.execute(
            f"INSERT INTO daily_meals (user_id, date, payload, created_at) VALUES ({m.PH},{m.PH},{m.PH},{m.PH})",
            ("fu-oil", today, json.dumps(payload), today))
        conn.commit(); conn.close()
    try:
        d = m._collect_feature_usage()
        keys = {t["key"] for t in d["meal_types"]}
        assert "manual_b" not in keys
        assert not any("Bを手動" in t["label"] or "Bだけ" in t["label"] for t in d["meal_types"])
        assert next(t for t in d["meal_types"] if t["key"] == "text")["records"] >= 1
    finally:
        with m._db_lock:
            conn = m._get_conn(); cur = conn.cursor()
            cur.execute(f"DELETE FROM daily_meals WHERE user_id={m.PH}", ("fu-oil",))
            conn.commit(); conn.close()


def test_dashboard_has_no_manual_b_column():
    """管理画面の表に「Bだけ」の列・説明が残っていないこと。"""
    with open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8") as f:
        html = f.read()
    assert "x.manual_b" not in html
    assert "🍙Bだけ" not in html
    assert "「Bだけ追加」" not in html
