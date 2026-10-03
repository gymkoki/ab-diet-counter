"""「小鉢の数：減量できている人 vs できていない人」の回帰テスト。

オーナーの仮説（2026-10-03）：
  減量に苦戦している人は小鉢が6個ほど並んでいて、知らず知らずのうちにB食材が増えているのでは。
  減量希望者の中で、減量達成群とそうでない群とで、小鉢の数に差はあるか。

固定する不変条件：
  ①皿の数は「行の数」ではなく「皿の数」で数える（油・調味料・飲み物は数えない／
    AIが1皿を分けた行は1皿にまとめる／普通の料理名を誤ってまとめない）
  ②1日の要約（daily_nutrition）に皿の数が残る（明細は5日で消えるため）
  ③比較は減量希望者だけ・成功群／失敗群の分け方はレポート④と同じ・管理者専用
  ④既存の「🔬 減量できている人とできていない人の違い」の比較項目は変えない
"""
import datetime
import json
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}


def f(name, cat="A", b=0, kcal=50):
    return {"name": name, "category": cat, "b_count": b, "kcal_per_serving": kcal}


# ── ① 皿の数の数え方 ─────────────────────────────────────
def test_helpers_and_drinks_are_not_dishes():
    foods = [f("白米", "B", 1, 250), f("味噌汁（豆腐・わかめ）"), f("焼き魚"),
             f("揚げ油（吸収油）", "B", 0.5, 150), f("ごまドレッシング", "B", 0, 60),
             f("中濃ソース", "B", 0, 20), f("醤油"), f("緑茶", kcal=0), f("水", kcal=0),
             f("バター", "B", 0, 70)]
    dishes, b_dishes, small_b, small_kcal = m._count_meal_dishes(foods)
    assert dishes == 3, "白米・味噌汁・焼き魚の3皿のはず（油・調味料・飲み物は数えない）"
    assert b_dishes == 1
    assert small_b == 0, "調味料のB0は「小さなB食材（小鉢）」に数えない"


def test_split_rows_of_one_dish_are_merged():
    """AIが1皿を分けた行（本体と揚げ油、豚汁の豚バラと具・汁）は1皿にまとめる。"""
    foods = [f("コロッケ本体（じゃがいも・ひき肉）", "B", 0.5, 150),
             f("コロッケ揚げ油（吸収油）", "B", 0.5, 140),
             f("豚汁の豚バラ", "B", 0, 110),
             f("豚汁の具・汁（大根・人参・こんにゃく・味噌）", "A", 0, 50)]
    dishes, b_dishes, small_b, small_kcal = m._count_meal_dishes(foods)
    assert dishes == 2, "コロッケ・豚汁の2皿のはず"
    assert b_dishes == 2
    assert small_b == 1 and small_kcal == 110, "豚汁の豚バラ（B食材・1品ではB0）が小さなB食材"


def test_ordinary_dish_names_are_not_merged_or_dropped():
    """「鶏の唐揚げ」「豚の生姜焼き」を誤ってまとめない／「じゃがバター」「水餃子」を消さない。"""
    foods = [f("鶏の唐揚げ", "B", 1, 250), f("豚の生姜焼き", "B", 1, 280),
             f("じゃがバター", "B", 0, 110), f("水餃子", "B", 0.5, 150)]
    dishes, *_ = m._count_meal_dishes(foods)
    assert dishes == 4


def test_many_small_bowls_case():
    """オーナーが見た「小鉢が6個」のような食事：小さなB食材が積み重なる。"""
    foods = [f("白米", "B", 1, 250), f("豚汁の豚バラ", "B", 0, 100), f("豚汁の具・汁"),
             f("きんぴらごぼう", "B", 0, 90), f("ポテトサラダ", "B", 0, 110),
             f("卵焼き", "B", 0, 100), f("ほうれん草のおひたし"), f("冷奴")]
    dishes, b_dishes, small_b, small_kcal = m._count_meal_dishes(foods)
    assert dishes == 7
    assert b_dishes == 5
    assert small_b == 4 and small_kcal == 400, "1品ずつはB0でも合計400kcalの見えないBがある"


# ── ② 1日の要約に残る ───────────────────────────────────
def test_day_summary_keeps_dish_counts():
    payload = json.dumps({"food": {"items": [
        {"result": {"foods": [f("白米", "B", 1, 250), f("焼き魚"), f("味噌汁")], "total_b_count": 1}},
        {"result": {"foods": [f("パスタ", "B", 1, 350), f("サラダ"), f("ドレッシング", "B", 0, 60)],
                    "total_b_count": 1}},
    ]}})
    s = m._summarize_day_meals(payload)
    assert s["dish_count"] == 5 and s["b_dish_count"] == 2
    assert s["small_b_count"] == 0


# ── ③ 比較（減量希望者だけ・成功群／失敗群） ───────────────────────
def _meal(n_dishes, n_small_b):
    foods = [f("白米", "B", 1, 250)]
    for k in range(n_small_b):
        foods.append(f(f"小鉢B{k}", "B", 0, 100))
    for k in range(max(0, n_dishes - 1 - n_small_b)):
        foods.append(f(f"小鉢A{k}", "A", 0, 30))
    return {"result": {"foods": foods, "total_b_count": 1, "total_protein_g": 20, "total_veg_g": 60}}


@pytest.fixture
def seeded():
    random.seed(7)
    m.init_db()
    today = datetime.datetime.now(m.JST).date()
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("daily_weight", "daily_meals", "daily_nutrition", "user_profile"):
        cur.execute(f"DELETE FROM {t}")

    def add(uid, goal, slope30, dishes, small_b):
        cur.execute(f"INSERT INTO user_profile (user_id, display_name, goal, updated_at) "
                    f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, uid, goal, today.isoformat()))
        for i in range(0, 40, 4):
            d = (today - datetime.timedelta(days=i)).isoformat()
            w = 65 - slope30 * (i / 30) + random.gauss(0, 0.1)
            cur.execute(f"INSERT INTO daily_weight (user_id,date,weight,created_at) "
                        f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, d, w, d))
        for i in range(4):
            d = (today - datetime.timedelta(days=i)).isoformat()
            items = [_meal(dishes, small_b) for _ in range(3)]
            cur.execute(f"INSERT INTO daily_meals (user_id,date,payload,created_at) "
                        f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})",
                        (uid, d, json.dumps({"food": {"items": items}}), d))

    # 会員ごとに少しばらつかせる（全員同じ値だと分散0で t 検定が計算できない）
    for k in range(5):
        add(f"ok-{k}", "cut", -1.2, 3 + k % 2, 0)    # 減量できている：1食3〜4皿・小さなBなし
    for k in range(5):
        add(f"ng-{k}", "cut", +0.6, 6 + k % 2, 3)    # できていない：1食6〜7皿・小さなB 3品
    add("keep-0", "maintain", +1.0, 9, 5)        # 体重維持の会員は比較に入れない
    conn.commit()
    conn.close()
    yield
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("daily_weight", "daily_meals", "daily_nutrition", "user_profile"):
        cur.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()


@pytest.fixture
def client():
    with m.app.test_client() as c:
        yield c


def test_dish_analysis_requires_admin(client):
    assert client.get("/api/admin/dish-analysis").status_code == 404


def test_dish_analysis_compares_cut_members_only(client, seeded):
    r = client.get("/api/admin/dish-analysis", headers=ADMIN)
    assert r.status_code == 200
    d = r.get_json()
    assert d["params"]["goal"] == "cut"
    assert d["coverage"]["loss_n"] == 5 and d["coverage"]["gain_n"] == 5, "維持の会員が混ざっている"
    by = {x["key"]: x for x in d["results"]}
    dish = by["dishes_per_meal"]
    assert abs(dish["loss"]["mean"] - 3.4) < 1e-6       # (3+4+3+4+3)/5
    assert abs(dish["gain"]["mean"] - 6.4) < 1e-6       # (6+7+6+7+6)/5
    assert dish["diff"] < 0 and dish["significant"], "約3皿 vs 約6皿の差が検出されていない"
    assert dish["per"] == "1食あたり"
    assert "減量できている人の方が1食あたり" in dish["summary"]
    small = by["small_b_kcal"]
    assert abs(small["gain"]["mean"] - 900.0) < 1e-6, "1日3食×小さなB3品×100kcal＝900kcal"
    assert d["dish_coverage"]["days"] > 0


def test_existing_diet_panel_is_unchanged():
    """既存パネルの比較項目（＝Holm補正の対象）は増やさない。"""
    assert [k for k, *_ in m.DIET_METRICS] == ["kcal", "veg_g", "protein_g", "b_count", "fruit_g", "meals"]
    keys = {mt[0] for mt in m.DISH_METRICS}
    assert {"dishes_per_meal", "b_dishes_per_meal", "small_b_count", "small_b_kcal"} <= keys


def test_admin_panel_exists_and_is_not_in_member_app():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    admin = open(os.path.join(root, "templates", "admin.html"), encoding="utf-8").read()
    front = open(os.path.join(root, "templates", "index.html"), encoding="utf-8").read()
    assert 'id="dish-analysis"' in admin and "loadDishAnalysis()" in admin
    assert "dish-analysis" not in front
