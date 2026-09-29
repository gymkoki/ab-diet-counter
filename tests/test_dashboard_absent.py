# 「食事記録が2週間以上ない人はダッシュボードの解析から外す」の回帰テスト。
# オーナー指示 2026-09-29（注目メンバーに、食事を2週間以上記録していない人が
# 「特に痩せていない人」として出ていた）。
#
# 守りたい不変条件：
#   ①注目メンバー・減量メンバーの詳細・B×体重の相関から、食事記録が14日以上ない人を外す
#   ②13日前までに記録がある人は外さない（境界）
#   ③一度も食事の記録が無くても、入会直後（アプリを最近使った）人は外さない
#   ④目的別の平均体重変化（ジムの実績KPI）は外さない（オーナー方針 2026-09-09）
#   ⑤DBが読めないときは絞り込まない（表示を丸ごと空にしない）

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
PREFIX = "dash-absent-"
ACTIVE = PREFIX + "active"      # 3日前に食事を記録
ABSENT = PREFIX + "absent"      # 最後の食事記録が20日前
EDGE13 = PREFIX + "edge13"      # 最後の食事記録が13日前
EDGE14 = PREFIX + "edge14"      # 最後の食事記録が14日前
NEWBIE = PREFIX + "newbie"      # 食事記録なし・2日前にアプリを使った
GONE   = PREFIX + "gone"        # 食事記録なし・30日前にアプリを使った


def _d(days_ago):
    return (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=days_ago)).isoformat()


def _exec(sql, params=()):
    with m._db_lock:
        conn = m._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


def _cleanup():
    for t in ("daily_b_count", "daily_weight", "user_profile"):
        _exec(f"DELETE FROM {t} WHERE user_id LIKE {m.PH}", (PREFIX + "%",))


def _profile(uid, goal, used_days_ago):
    ts = f"{_d(used_days_ago)}T12:00:00+09:00"
    _exec(f"INSERT INTO user_profile (user_id, display_name, is_vip, updated_at, goal) "
          f"VALUES ({m.PH},{m.PH},0,{m.PH},{m.PH})", (uid, uid, ts, goal))


def _meal(uid, days_ago, b=3.0):
    _exec(f"INSERT INTO daily_b_count (user_id, date, b_count, created_at) "
          f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, _d(days_ago), b, _d(days_ago)))


def _weights(uid, start_kg, end_kg, days=30):
    """days 日前 → 今日 で start_kg → end_kg に変化する体重記録を入れる。"""
    for i, ago in enumerate((days, days // 2, 0)):
        w = start_kg + (end_kg - start_kg) * i / 2
        _exec(f"INSERT INTO daily_weight (user_id, date, weight, created_at) "
              f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, _d(ago), w, _d(ago)))


@pytest.fixture
def client():
    m.init_db()
    _cleanup()
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c
    _cleanup()


# ── 判定そのもの ────────────────────────────────────────────────
def test_active_uids_boundaries(client):
    _profile(ACTIVE, "cut", 3); _meal(ACTIVE, 3)
    _profile(ABSENT, "cut", 20); _meal(ABSENT, 20)
    _profile(EDGE13, "cut", 13); _meal(EDGE13, 13)
    _profile(EDGE14, "cut", 14); _meal(EDGE14, 14)
    _profile(NEWBIE, "cut", 2)
    _profile(GONE, "cut", 30)

    active = m._dashboard_active_uids()
    assert ACTIVE in active
    assert EDGE13 in active, "13日前に記録がある人まで外している"
    assert ABSENT not in active, "20日記録が無い人が残っている"
    assert EDGE14 not in active, "14日（2週間）記録が無い人が残っている"
    assert NEWBIE in active, "入会直後でまだ記録していない人まで外している"
    assert GONE not in active, "食事記録が無く、アプリも1か月使っていない人が残っている"


def test_meal_record_wins_over_recent_app_use(client):
    """アプリを開いていても、食事を2週間以上記録していなければ外す。"""
    _profile(ABSENT, "cut", 1)      # 昨日アプリは開いた
    _meal(ABSENT, 20)               # 食事の記録は20日前が最後
    assert ABSENT not in m._dashboard_active_uids()


def test_db_failure_does_not_filter(monkeypatch):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "_get_conn", _boom)
    assert m._dashboard_active_uids() is None


# ── 注目メンバー ────────────────────────────────────────────────
def test_spotlight_excludes_absent_members(client):
    # どちらも極端に体重が増えた減量希望者。絞り込みが無ければ真っ先に「痩せていない人」に入る
    _profile(ACTIVE, "cut", 3);  _meal(ACTIVE, 3);  _weights(ACTIVE, 60, 90)
    _profile(ABSENT, "cut", 20); _meal(ABSENT, 20); _weights(ABSENT, 60, 95)
    d = client.get("/api/admin/spotlight", headers=ADMIN).get_json()
    shown = {u["user_id"] for u in d["lost"] + d["not_lost"]}
    assert ACTIVE in shown
    assert ABSENT not in shown, "食事記録が2週間以上ない人が注目メンバーに出ている"


def test_spotlight_excludes_absent_from_lost_group(client):
    _profile(ACTIVE, "cut", 3);  _meal(ACTIVE, 3);  _weights(ACTIVE, 90, 60)
    _profile(ABSENT, "cut", 20); _meal(ABSENT, 20); _weights(ABSENT, 95, 60)
    d = client.get("/api/admin/spotlight", headers=ADMIN).get_json()
    lost = {u["user_id"] for u in d["lost"]}
    assert ACTIVE in lost
    assert ABSENT not in lost


# ── 減量メンバーの詳細 ──────────────────────────────────────────
def test_cut_members_excludes_absent(client):
    _profile(ACTIVE, "cut", 3);  _meal(ACTIVE, 3)
    _profile(ABSENT, "cut", 20); _meal(ABSENT, 20)
    _profile(NEWBIE, "cut", 2)
    d = client.get("/api/admin/cut-members", headers=ADMIN).get_json()
    ids = {mem["user_id"] for mem in d["members"]}
    assert ACTIVE in ids and NEWBIE in ids
    assert ABSENT not in ids


# ── 体重インサイト ──────────────────────────────────────────────
def test_correlation_excludes_absent_but_kpi_keeps_them(client):
    # 30日間の体重記録と食事記録。ABSENT は途中（20日前）で食事の記録をやめている
    _profile(ACTIVE, "cut", 3);  _meal(ACTIVE, 3);  _meal(ACTIVE, 25); _weights(ACTIVE, 70, 68, days=28)
    _profile(ABSENT, "cut", 20); _meal(ABSENT, 20); _meal(ABSENT, 25); _weights(ABSENT, 70, 72, days=28)
    d = client.get("/api/admin/weight-insights", headers=ADMIN).get_json()

    names = {p.get("name") for p in d["scatter"]}
    assert ACTIVE in names, "記録を続けている人が相関の散布図から消えている"
    assert ABSENT not in names, "食事記録が2週間以上ない人が相関の散布図に入っている"
    assert all("_uid" not in p for p in d["scatter"]), "内部用のIDが応答に漏れている"

    # 目的別の平均体重変化（KPI）には2人とも入ったまま
    assert d["goals"]["cut"]["users"] >= 2, "ジムの実績KPIからまで外している"


# ── 画面の説明文 ────────────────────────────────────────────────
def test_dashboard_explains_the_exclusion():
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "templates", "admin.html"), encoding="utf-8") as f:
        html = f.read()
    assert html.count("食事の記録が2週間以上ない人") >= 3, \
        "注目メンバー・減量メンバー・相関の説明に除外の条件が書かれていない"
