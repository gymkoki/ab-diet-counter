"""減量群 vs 増量群 の食事比較（t 検定）の回帰テスト。

オーナー指示 2026-09-29：
  減量できている人とできていない人で、摂取カロリー・野菜・タンパク質などを
  t 検定して違いを出す。その日の食事が3食に満たない日は解析から除外する。

守りたい不変条件：
  ①統計の計算が正しいこと（SciPy の結果と一致。本番に SciPy を入れずに済むよう純Pythonで実装）
  ②比べる単位は「会員」。記録の多い人が結果を支配しないこと
  ③3食未満の日は必ず除外されること
  ④仕込んだ差（野菜）を検出し、仕込んでいない差（カロリー等）を「有意」と言わないこと
  ⑤食事の明細が5日で消えても、栄養の合計は残ること
  ⑥管理者以外は見られないこと
"""
import datetime
import json
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402
import diet_stats as ds  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}


# ── ① 統計の計算（SciPy 1.x で計算した参照値）─────────────────────
A = [310, 280, 250, 420, 390]
B = [150, 170, 120, 210]


def test_welch_matches_scipy_reference():
    r = ds.welch_ttest(A, B)
    # scipy.stats.ttest_ind(A, B, equal_var=False)
    assert r["t"] == pytest.approx(4.466666666666667, rel=1e-9)
    assert r["p"] == pytest.approx(0.003894404552884286, rel=1e-7)
    # .confidence_interval(0.95)
    assert r["ci_low"] == pytest.approx(76.5219867838546, rel=1e-7)
    assert r["ci_high"] == pytest.approx(258.4780132161454, rel=1e-7)


@pytest.mark.parametrize("t,df,expected", [
    (0.0, 5, 1.0),
    (50.0, 3, 1.76172e-05),
    (-2.1, 1.2, 0.248755),
    (1.96, 1e6, 0.0499961),
])
def test_t_distribution_matches_scipy(t, df, expected):
    assert ds.t_two_sided_p(t, df) == pytest.approx(expected, rel=1e-5)


def test_holm_adjustment():
    got = ds.holm([0.01, 0.04, 0.03, 0.20, None])
    assert got[:4] == pytest.approx([0.04, 0.09, 0.09, 0.2])
    assert got[4] is None


def test_cannot_test_with_fewer_than_two_per_group():
    assert ds.welch_ttest([1.0], [2.0, 3.0]) is None
    assert ds.hedges_g([1.0, 2.0], [3.0]) is None


def test_slope():
    assert ds.slope_per_day([(0, 60.0), (30, 59.0)]) == pytest.approx(-1 / 30)
    assert ds.slope_per_day([(5, 60.0), (5, 61.0)]) is None


# ── 1日ぶんの集計 ───────────────────────────────────────────────
def _meal(kcal, veg, protein, b, **flags):
    item = {"result": {"total_b_count": b, "total_protein_g": protein, "total_veg_g": veg,
                       "foods": [{"name": "x", "kcal_per_serving": kcal, "b_count": b}]}}
    item.update(flags)
    return item


def test_summary_adds_kcal_and_b_without_changing_meal_count():
    """kcal・Bカウントを足しても、件数の数え方（共通定義）は変えないこと。

    件数は _summarize_day_meals の1か所で決める約束（画面ごとに数字がずれないように）。
    """
    payload = {"food": {"items": [
        _meal(500, 100, 20, 2), _meal(600, 120, 30, 2),
        _meal(135, 0, 0, 0.5, isOil=True),          # 手動追加（油）
        {"loading": True},                            # 解析前の項目は数えない
    ]}}
    s = m._summarize_day_meals(json.dumps(payload))
    assert s["meal_count"] == 3                       # 共通定義のまま（解析結果のある項目数）
    assert s["kcal"] == pytest.approx(1235)
    assert s["b_count"] == pytest.approx(4.5)
    assert s["veg_g"] == pytest.approx(220)
    assert s["fruit_g"] is None                       # 未計測と0gを区別


def test_kcal_is_none_when_no_food_has_kcal():
    payload = {"food": {"items": [{"result": {"total_b_count": 1, "foods": [{"name": "x"}]}}]}}
    assert m._summarize_day_meals(json.dumps(payload))["kcal"] is None


def test_summarize_returns_none_without_results():
    assert m._summarize_day_meals(json.dumps({"food": {"items": [{"loading": True}]}})) is None
    assert m._summarize_day_meals("こわれたJSON") is None


def test_migration_adds_kcal_columns_to_existing_table(tmp_path, monkeypatch):
    """本番には kcal・b_count 列の無い daily_nutrition が既にある。起動時に列が足されること。"""
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE daily_nutrition (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, date TEXT NOT NULL,
        meal_count INTEGER NOT NULL, protein_g REAL, veg_g REAL, fruit_g REAL,
        created_at TEXT NOT NULL, UNIQUE(user_id, date))""")
    con.execute("INSERT INTO daily_nutrition (user_id,date,meal_count,protein_g,veg_g,fruit_g,created_at) "
                "VALUES ('u','2026-09-01',3,60,200,NULL,'x')")
    con.commit()
    con.close()
    monkeypatch.setattr(m, "USE_PG", False)
    monkeypatch.setattr(m, "_get_conn", lambda: sqlite3.connect(db))
    m.init_db()
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(daily_nutrition)")}
    old_row = con.execute("SELECT meal_count, kcal FROM daily_nutrition").fetchone()
    con.close()
    assert {"kcal", "b_count"} <= cols, "既存テーブルに列が追加されていません"
    assert old_row == (3, None), "既存の行が壊れています（kcalは未計測=NULLのはず）"


# ── ②③④ 解析全体（答えの分かっている合成データで確認）───────────────
@pytest.fixture
def seeded():
    """減量群6人は野菜350g、増量群6人は野菜200g。カロリー・タンパク質は同じ。
    各自1日だけ3食未満の日を混ぜ、それが除外されるかも確かめる。"""
    random.seed(3)
    m.init_db()
    today = datetime.datetime.now(m.JST).date()
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("daily_weight", "daily_meals", "daily_nutrition", "user_profile"):
        cur.execute(f"DELETE FROM {t}")

    def add(uid, slope30, veg_day, pattern):
        for i in range(0, 40, 4):
            d = (today - datetime.timedelta(days=i)).isoformat()
            w = 65 - slope30 * (i / 30) + random.gauss(0, 0.15)
            cur.execute(f"INSERT INTO daily_weight (user_id,date,weight,created_at) "
                        f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, d, w, d))
        for i, n in enumerate(pattern):
            d = (today - datetime.timedelta(days=i)).isoformat()
            items = [_meal(600 + random.gauss(0, 60), veg_day / 3 + random.gauss(0, 15), 25, 2)
                     for _ in range(n)]
            cur.execute(f"INSERT INTO daily_meals (user_id,date,payload,created_at) "
                        f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})",
                        (uid, d, json.dumps({"food": {"items": items}}), d))

    for k in range(6):
        add(f"loss-{k}", -1.2, 350 + random.gauss(0, 30), [3, 3, 4, 3, 1])
    for k in range(6):
        add(f"gain-{k}", +0.9, 200 + random.gauss(0, 30), [3, 4, 3, 2, 3])
    conn.commit()
    conn.close()
    yield
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("daily_weight", "daily_meals", "daily_nutrition"):
        cur.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()


def _by_key(r):
    return {x["key"]: x for x in r["results"]}


def test_groups_are_formed_by_weight_trend(seeded):
    r = m._diet_analysis()
    assert r["coverage"]["loss_n"] == 6
    assert r["coverage"]["gain_n"] == 6


def test_days_with_fewer_than_three_meals_are_excluded(seeded):
    """オーナー指示：その日3食分ない日は除外。1食・2食の日が1人1日ずつ＝12日除外。"""
    r = m._diet_analysis()
    assert r["coverage"]["days_excluded"] == 12
    assert r["coverage"]["days_used"] == 48
    for mem in r["members"]:
        assert mem["days"] == 4, "3食未満の日が平均に混ざっています"
        assert mem["means"]["meals"] >= 3


def test_detects_the_planted_vegetable_difference(seeded):
    veg = _by_key(m._diet_analysis())["veg_g"]
    assert veg["significant"] is True
    assert veg["diff"] == pytest.approx(150, abs=40), "仕込んだ差（約150g）と大きくずれています"
    assert veg["diff"] > 0, "減量群の方が多いはずです"
    assert "減量群の方が" in veg["summary"] and "多い" in veg["summary"]


def test_does_not_invent_differences(seeded):
    """仕込んでいない項目を「有意」と言わないこと（Holm 補正の効き目）。"""
    res = _by_key(m._diet_analysis())
    for key in ("kcal", "protein_g", "b_count"):
        assert res[key]["significant"] is False, f"{key} に偽の有意差が出ています"


def test_headline_mentions_only_real_findings(seeded):
    head = m._diet_analysis()["headline"]
    assert any("野菜" in h for h in head)
    assert not any("タンパク質" in h for h in head)


def test_unit_of_analysis_is_the_member(seeded):
    """1人が大量の日数を記録しても、その人は「1人」として扱われること。"""
    base = m._diet_analysis()
    today = datetime.datetime.now(m.JST).date()
    conn = m._get_conn()
    cur = conn.cursor()
    for i in range(5, 40):   # gain-0 だけ35日ぶん追加で記録
        d = (today - datetime.timedelta(days=i)).isoformat()
        cur.execute(f"INSERT INTO daily_nutrition (user_id,date,meal_count,kcal,b_count,protein_g,veg_g,fruit_g,created_at) "
                    f"VALUES ({m.PH},{m.PH},3,1800,6,75,200,NULL,{m.PH})", ("gain-0", d, d))
    conn.commit()
    conn.close()
    after = m._diet_analysis()
    assert after["coverage"]["gain_n"] == base["coverage"]["gain_n"], \
        "記録日数が多い人が、人数として重複して数えられています"


def test_flat_members_are_excluded_when_threshold_is_set(seeded):
    r = m._diet_analysis(threshold=5.0)   # 全員が±5kg/30日の内側＝維持群
    assert r["coverage"]["loss_n"] == 0 and r["coverage"]["gain_n"] == 0
    assert r["coverage"]["flat_excluded"] == 12
    assert "比較できません" in _by_key(r)["veg_g"]["summary"]


# ── ⑤ 明細が消えても数値は残る ───────────────────────────────────
def test_nutrition_survives_meal_purge():
    """写真付きの明細は5日で消えるが、栄養の合計は残ること。"""
    m.init_db()
    uid = "purge-user"
    old_day = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=30)).isoformat()
    c = m.app.test_client()
    payload = {"food": {"items": [_meal(500, 100, 20, 2)] * 3}}
    assert c.post("/api/daily-meals", json={"user_id": uid, "date": old_day,
                                            "payload": payload}).status_code == 200
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM daily_meals WHERE user_id={m.PH} AND date={m.PH}", (uid, old_day))
        assert cur.fetchone()[0] == 0, "前提：5日より古い明細は消えるはず"
        cur.execute(f"SELECT meal_count, veg_g, kcal FROM daily_nutrition WHERE user_id={m.PH} AND date={m.PH}",
                    (uid, old_day))
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, "明細と一緒に栄養の合計まで消えています"
    assert row[0] == 3 and row[1] == pytest.approx(300)
    assert row[2] == pytest.approx(1500), "摂取カロリーが保存されていません"


# ── ⑥ 管理者だけ ────────────────────────────────────────────────
def test_admin_only():
    c = m.app.test_client()
    assert c.get("/api/admin/diet-analysis").status_code == 404
    assert c.get("/api/admin/diet-analysis", headers=ADMIN).status_code == 200


def test_dashboard_has_the_card():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "templates", "admin.html"), encoding="utf-8") as f:
        html = f.read()
    assert "/api/admin/diet-analysis" in html
    assert "loadDietAnalysis()" in html
    assert "3食に満たない日" in html, "除外ルールが画面で説明されていません"


def test_rows_written_before_kcal_existed_get_filled():
    """本番には kcal 列を足す前に書かれた行（kcal=NULL）がある。
    明細が残っている日（直近5日）は、解析のときに kcal が埋まること。"""
    m.init_db()
    uid = "legacy-user"
    day = datetime.datetime.now(m.JST).date().isoformat()
    payload = json.dumps({"food": {"items": [_meal(700, 100, 25, 2)] * 3}})
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM daily_nutrition WHERE user_id={m.PH}", (uid,))
        cur.execute(f"DELETE FROM daily_meals WHERE user_id={m.PH}", (uid,))
        # 旧コードが書いた行を再現：件数・野菜はあるが kcal・B は NULL
        cur.execute(f"INSERT INTO daily_nutrition (user_id,date,meal_count,protein_g,veg_g,fruit_g,created_at) "
                    f"VALUES ({m.PH},{m.PH},3,75,300,NULL,{m.PH})", (uid, day, day))
        cur.execute(f"INSERT INTO daily_meals (user_id,date,payload,created_at) "
                    f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, day, payload, day))
        conn.commit()
    finally:
        conn.close()

    m._refresh_nutrition_kcal()

    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT kcal, b_count, meal_count FROM daily_nutrition WHERE user_id={m.PH} AND date={m.PH}",
                    (uid, day))
        kcal, b, meals = cur.fetchone()
    finally:
        conn.close()
    assert kcal == pytest.approx(2100), "旧データの kcal が埋まっていません"
    assert b == pytest.approx(6)
    assert meals == 3
    # 2回目は対象が無い（＝毎回全部を計算し直さない）
    assert m._refresh_nutrition_kcal() == 0
