# 「2kg以上減量できた人は何を食べていたか」の集計の回帰テスト。
#
# オーナー依頼 2026-09-29：
#   減量達成者（2kg以上）の平均Bカウント・野菜・タンパク質を知りたい。
#   ただし「1日1枚しか写真を出していない人」に平均が引っ張られるため、
#   基本的に2食分以上アップしている人のみを対象にすること。
#
# ここで固定する不変条件：
#   ①1日1件しか記録が無い日は集計に入れない（min_meals=2）
#   ②平均は「会員1人＝1票」（記録の多い人が平均を支配しない）
#   ③栄養サマリー(daily_nutrition)は daily_meals の5日削除では消えない
#   ④管理者認証が要る（会員アプリからは見えない）

import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
os.environ.setdefault("ADMIN_PASSWORD", "testpw")

import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}


def _reset():
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        for t in ("user_profile", "daily_weight", "daily_b_count", "daily_meals", "daily_nutrition"):
            cur.execute(f"DELETE FROM {t}")
        conn.commit()
    finally:
        conn.close()
    m._nutrition_backfilled = False


@pytest.fixture
def client():
    m.init_db()
    m.app.config["TESTING"] = True
    _reset()
    with m.app.test_client() as c:
        yield c
    _reset()


def _payload(n_items, protein=20, veg=100):
    """n_items 件の食事を記録した1日ぶんのpayload。"""
    return {"food": {"items": [
        {"id": i, "result": {"foods": [{"name": "食材", "protein_g": protein, "veg_g": veg}],
                             "total_protein_g": protein, "total_veg_g": veg, "total_b_count": 1}}
        for i in range(n_items)
    ]}}


def _seed_member(uid, name, first_w, last_w, days):
    """days: [(日付, 記録件数, タンパク質, 野菜, Bカウント), ...]"""
    now = datetime.datetime.now(m.JST)
    ts = now.isoformat()
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        PH = m.PH
        cur.execute(f"INSERT INTO user_profile (user_id, display_name, goal, updated_at) "
                    f"VALUES ({PH},{PH},'cut',{PH})", (uid, name, ts))
        for i, w in enumerate((first_w, last_w)):
            cur.execute(f"INSERT INTO daily_weight (user_id,date,weight,created_at) "
                        f"VALUES ({PH},{PH},{PH},{PH})",
                        (uid, f"2026-09-{1 + i * 10:02d}", w, ts))
        for date, n_items, protein, veg, b in days:
            cur.execute(f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) "
                        f"VALUES ({PH},{PH},{PH},{PH})", (uid, date, b, ts))
            m._save_daily_nutrition(cur, uid, date,
                                    json.dumps(_payload(n_items, protein, veg)), ts)
        conn.commit()
    finally:
        conn.close()


# ── ① 1日1件だけの日は集計から外す ──────────────────────────
def test_single_meal_days_are_excluded():
    """1日1件しか記録が無い日は「食べた量」ではないので平均に入れない。"""
    _reset()
    # 同じ人が、2件記録した日（タンパク質80g）と1件だけの日（20g）を持つ
    _seed_member("u-1", "テスト", 70.0, 67.0, [
        ("2026-09-20", 2, 40, 150, 3.0),   # 合計 80g / 300g
        ("2026-09-21", 1, 20, 50, 1.0),    # ← 除外されるべき
    ])
    d = m.collect_achiever_nutrition()
    a = d["achievers"]
    assert a["members"] == 1
    assert a["days"] == 1, "1件だけの日が集計に混ざっています"
    assert a["avg_protein_g"] == 80.0
    assert a["avg_veg_g"] == 300.0
    assert a["avg_b"] == 3.0

    # min_meals=1 にすれば、1件だけの日も入る（比較用）
    d1 = m.collect_achiever_nutrition(min_meals=1)
    assert d1["achievers"]["days"] == 2
    assert d1["achievers"]["avg_protein_g"] == 50.0   # (80 + 20) / 2


# ── ② 達成者の線引き（2kg以上） ─────────────────────────────
def test_achiever_threshold_is_2kg():
    _reset()
    _seed_member("u-win", "達成", 70.0, 68.0, [("2026-09-20", 2, 50, 200, 2.0)])   # ちょうど2.0kg減
    _seed_member("u-near", "あと少し", 70.0, 68.1, [("2026-09-20", 2, 30, 80, 5.0)])  # 1.9kg減
    d = m.collect_achiever_nutrition()
    assert d["achievers"]["members"] == 1 and d["others"]["members"] == 1
    assert d["achievers"]["avg_protein_g"] == 100.0   # 50g × 2件
    assert d["others"]["avg_protein_g"] == 60.0       # 30g × 2件
    assert [x["name"] for x in d["achiever_members"]] == ["達成"]


def test_members_without_two_weights_are_skipped():
    """体重が1回しか無い人は増減が分からないので、どちらの群にも入れない。"""
    _reset()
    now = datetime.datetime.now(m.JST).isoformat()
    conn = m._get_conn()
    try:
        cur = conn.cursor(); PH = m.PH
        cur.execute(f"INSERT INTO user_profile (user_id, display_name, goal, updated_at) "
                    f"VALUES ({PH},'体重1回','cut',{PH})", ("u-one", now))
        cur.execute(f"INSERT INTO daily_weight (user_id,date,weight,created_at) "
                    f"VALUES ({PH},'2026-09-01',70.0,{PH})", ("u-one", now))
        m._save_daily_nutrition(cur, "u-one", "2026-09-20", json.dumps(_payload(3)), now)
        conn.commit()
    finally:
        conn.close()
    d = m.collect_achiever_nutrition()
    assert d["achievers"]["members"] == 0 and d["others"]["members"] == 0


# ── ③ 平均は「会員1人＝1票」 ────────────────────────────────
def test_average_is_one_vote_per_member():
    """記録日数が多い会員が平均を支配しないこと。"""
    _reset()
    # よく記録する人（10日・タンパク質20g）と、たまに記録する人（1日・タンパク質100g）
    _seed_member("u-many", "たくさん記録", 80.0, 75.0,
                 [(f"2026-09-{d:02d}", 2, 10, 50, 2.0) for d in range(10, 20)])
    _seed_member("u-few", "たまに記録", 80.0, 75.0, [("2026-09-20", 2, 50, 300, 4.0)])
    d = m.collect_achiever_nutrition()
    a = d["achievers"]
    assert a["members"] == 2 and a["days"] == 11
    # 1人1票なら (20 + 100) / 2 = 60。日数で重みづけすると 27g 前後になる。
    assert a["avg_protein_g"] == 60.0, "記録日数の多い会員に平均が引っ張られています"
    assert a["avg_veg_g"] == 350.0    # (100 + 600) / 2


# ── ④ 栄養サマリーは5日削除で消えない ───────────────────────
def test_nutrition_summary_survives_meal_purge(client):
    """daily_meals は5日で消えるが、daily_nutrition は残ること。"""
    old_date = (datetime.datetime.now(m.JST) - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    today    = datetime.datetime.now(m.JST).strftime("%Y-%m-%d")
    for date in (old_date, today):
        r = client.post("/api/daily-meals", json={
            "user_id": "u-purge", "date": date, "payload": _payload(2, 30, 120)})
        assert r.status_code == 200

    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT date FROM daily_meals WHERE user_id={m.PH}", ("u-purge",))
        meals = {r[0] for r in cur.fetchall()}
        cur.execute(f"SELECT date, meal_count, protein_g, veg_g FROM daily_nutrition "
                    f"WHERE user_id={m.PH} ORDER BY date", ("u-purge",))
        nut = cur.fetchall()
    finally:
        conn.close()
    assert old_date not in meals, "5日より前の明細が残っています（保持方針が変わりました）"
    assert {r[0] for r in nut} == {old_date, today}, "栄養サマリーまで消えています"
    assert nut[0][1] == 2 and nut[0][2] == 60.0 and nut[0][3] == 240.0


def test_summary_is_removed_when_all_records_are_deleted(client):
    """その日の記録を全部消したら、古い数字を残さないこと。"""
    today = datetime.datetime.now(m.JST).strftime("%Y-%m-%d")
    client.post("/api/daily-meals", json={"user_id": "u-del", "date": today, "payload": _payload(2)})
    client.post("/api/daily-meals", json={"user_id": "u-del", "date": today,
                                          "payload": {"food": {"items": []}}})
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM daily_nutrition WHERE user_id={m.PH}", ("u-del",))
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_backfill_fills_from_existing_meals():
    """導入時、まだ残っている明細から栄養サマリーを補完すること。"""
    _reset()
    now = datetime.datetime.now(m.JST).isoformat()
    today = datetime.datetime.now(m.JST).strftime("%Y-%m-%d")
    conn = m._get_conn()
    try:
        cur = conn.cursor(); PH = m.PH
        cur.execute(f"INSERT INTO daily_meals (user_id,date,payload,created_at) "
                    f"VALUES ({PH},{PH},{PH},{PH})",
                    ("u-back", today, json.dumps(_payload(3, 25, 90)), now))
        conn.commit()
    finally:
        conn.close()
    assert m.backfill_daily_nutrition() == 1
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT meal_count, protein_g, veg_g FROM daily_nutrition WHERE user_id={m.PH}",
                    ("u-back",))
        assert cur.fetchone() == (3, 75.0, 270.0)
    finally:
        conn.close()
    assert m.backfill_daily_nutrition() == 0, "2回目で二重に入れています"


# ── ⑤ 管理者専用であること ─────────────────────────────────
def test_endpoint_requires_admin(client):
    assert client.get("/api/admin/achiever-nutrition").status_code == 404
    assert client.get("/api/admin/achiever-nutrition", headers=ADMIN).status_code == 200


def test_endpoint_accepts_thresholds(client):
    _seed_member("u-a", "3kg減", 70.0, 67.0, [("2026-09-20", 2, 40, 200, 2.0)])
    r = client.get("/api/admin/achiever-nutrition?min_loss=5&min_meals=2", headers=ADMIN)
    d = r.get_json()
    assert d["min_loss_kg"] == 5.0 and d["achievers"]["members"] == 0
    assert d["others"]["members"] == 1


def test_dashboard_shows_the_card():
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "admin.html"), encoding="utf-8").read()
    assert "achiever-wrap" in html and "loadAchieverNutrition()" in html
    assert "1日に2件以上記録した日だけ" in html, "集計条件の説明がありません"
