"""野菜量の過大評価を防ぐ回帰テスト。

オーナー指摘 2026-10-01：
  水餃子5個＋ミニトマト2個ときゅうり数枚の小皿＋キャベツ・玉ねぎ・にんじんのスープが
  「野菜230g」と出た。実際は100g程度。他の食事にも同様に適用すること。

原因は3つ：
  ①スープの「汁」まで野菜に数えていた
  ②餃子の「具」に混ぜ込まれた野菜（キャベツ・ニラ）を数えていた
  ③ミニトマトを大きいトマト並みの重さで数えていた
対策：プロンプトに「1個の重さ×個数」「汁は数えない」「包み物の具は0」を入れたうえで、
サーバー側でもこの3つの型だけは上限をかける（_sanitize_veg・recompute_totals の中）。
"""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402
from conftest import seed_weight  # noqa: E402


def _food(name, veg, reason="", b=0.5):
    return {"name": name, "kcal_per_serving": 100, "amount": "", "b_count": b,
            "protein_g": 5, "veg_g": veg, "reason": reason}


def _total(foods):
    return m.recompute_totals({"foods": foods})["total_veg_g"]


# ── オーナーの実例 ──────────────────────────────────────────────
def test_owner_case_is_no_longer_230g():
    """AIが以前と同じ過大な内訳を返しても、合計は100g前後に収まること。"""
    bad = [
        _food("水餃子（皮・具5個）", 70, b=1),
        _food("ミニトマト2個", 50, b=0),
        _food("きゅうり", 25, b=0),
        _food("キャベツ・玉ねぎ・にんじんのスープ", 85, b=0),
    ]
    assert sum(f["veg_g"] for f in bad) == 230
    total = _total(bad)
    assert total <= 130, f"まだ過大です: {total}g（実際は100g程度）"
    assert total >= 80, f"削りすぎです: {total}g"


# ── ① 汁物は具だけ ──────────────────────────────────────────────
@pytest.mark.parametrize("name,veg,expected", [
    ("キャベツ・玉ねぎ・にんじんのスープ", 120, m.VEG_SOUP_MAX),
    ("わかめの味噌汁", 90, m.VEG_SOUP_MAX),
    ("コンソメスープ", 40, 40),                          # 上限より少なければそのまま
    ("豚汁", 200, m.VEG_HEARTY_SOUP_MAX),                 # 具だくさんは上限が高い
    ("具だくさんミネストローネ", 130, 130),
])
def test_soup_vegetables_are_capped(name, veg, expected):
    foods = [_food(name, veg)]
    m.recompute_totals({"foods": foods})
    assert foods[0]["veg_g"] == expected


def test_soupless_noodles_are_not_treated_as_soup():
    foods = [_food("汁なし担々麺（青梗菜たっぷり）", 90)]
    m.recompute_totals({"foods": foods})
    assert foods[0]["veg_g"] == 90


# ── ② 包み物・タネの中の野菜は0 ─────────────────────────────────
@pytest.mark.parametrize("name", [
    "水餃子（皮・具5個）", "焼き餃子6個", "焼売4個", "肉まん", "春巻き2本",
    "ハンバーグ", "メンチカツ", "コロッケ", "鶏つくね",
])
def test_vegetables_inside_fillings_are_not_counted(name):
    foods = [_food(name, 40)]
    m.recompute_totals({"foods": foods})
    assert foods[0]["veg_g"] == 0, f"{name} の具の野菜を数えています"


def test_garnish_lumped_with_main_dish_is_kept():
    """付け合わせが同じ項目にまとめられていたら、本物の野菜を消さない。"""
    foods = [_food("ハンバーグ（付け合わせのブロッコリー・にんじん）", 60)]
    m.recompute_totals({"foods": foods})
    assert foods[0]["veg_g"] == 60


# ── ③ ミニトマトは1個≒15gまで ──────────────────────────────────
@pytest.mark.parametrize("name,veg,expected", [
    ("ミニトマト2個", 60, 30),
    ("プチトマト3個", 100, 45),
    ("ミニトマト", 90, m.VEG_CHERRY_DEFAULT_MAX),       # 個数が読めないときは3個ぶんまで
    ("ミニトマト2個", 20, 20),                          # 少なければそのまま
])
def test_cherry_tomatoes_are_capped(name, veg, expected):
    foods = [_food(name, veg)]
    m.recompute_totals({"foods": foods})
    assert foods[0]["veg_g"] == expected


def test_salad_with_cherry_tomatoes_is_not_capped():
    """サラダにミニトマトが入っているだけなら、サラダ全体を削らない。"""
    foods = [_food("グリーンサラダ（レタス・ミニトマト2個・きゅうり）", 110)]
    m.recompute_totals({"foods": foods})
    assert foods[0]["veg_g"] == 110


@pytest.mark.parametrize("name", [
    "野菜炒め（スープ付き）", "焼き魚定食（味噌汁・小鉢）", "温野菜の盛り合わせ",
])
def test_combined_items_are_never_capped(name):
    foods = [_food(name, 180)]
    m.recompute_totals({"foods": foods})
    assert foods[0]["veg_g"] == 180


# ── 会員の訂正は尊重する ───────────────────────────────────────
def test_member_correction_is_respected():
    foods = [_food("キャベツたっぷりスープ", 150, reason="訂正反映：本人申告でキャベツ150g")]
    m.recompute_totals({"foods": foods})
    assert foods[0]["veg_g"] == 150


# ── 補正の記録と合計の整合 ─────────────────────────────────────
def test_adjustment_is_recorded_and_total_matches():
    foods = [_food("水餃子5個", 70), _food("きゅうりの浅漬け", 30)]
    r = m.recompute_totals({"foods": foods})
    assert foods[0]["veg_adjusted_from"] == 70, "元の値を残していません（後から追えない）"
    assert "veg_adjusted_from" not in foods[1]
    assert r["total_veg_g"] == 30 == sum(f["veg_g"] for f in foods)


# ── 実際の解析の入口を通しても効くこと ─────────────────────────
@pytest.fixture
def client(monkeypatch):
    m.app.config["TESTING"] = True
    monkeypatch.setattr(m, "get_client", lambda: object())
    seed_weight("veg-user")
    with m.app.test_client() as c:
        yield c


AI_REPLY = {
    "foods": [
        _food("水餃子（皮・具5個）", 70, b=1),
        _food("ミニトマト2個", 50, b=0),
        _food("きゅうり", 25, b=0),
        _food("キャベツ・玉ねぎ・にんじんのスープ", 85, b=0),
    ],
    "total_b_count": 1, "total_protein_g": 14, "total_veg_g": 230, "advice": "",
}


@pytest.mark.parametrize("path,data", [
    ("/analyze-text", {"text": "水餃子5個、ミニトマト、きゅうり、野菜スープ"}),
    ("/reanalyze", {"correction": "餃子は5個です"}),
])
def test_endpoints_apply_the_safety_net(client, monkeypatch, path, data):
    """AIが230gと返しても、会員の画面に届く値は補正済みであること。"""
    monkeypatch.setattr(m, "_create_with_server_tools", lambda *a, **k: None)
    monkeypatch.setattr(m, "parse_ai_result", lambda resp: copy.deepcopy(AI_REPLY))
    r = client.post(path, data={**data, "user_id": "veg-user"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["total_veg_g"] <= 130


# ── プロンプト（他の食事にも同じ考え方を適用）──────────────────
def test_analysis_prompt_has_the_new_rules():
    p = m.ANALYSIS_PROMPT
    assert "ミニトマト1個≒10〜15g" in p
    assert "汁そのものは0g" in p
    assert "包み物・タネの中の野菜は数えない" in p
    assert "迷ったら少なめ" in p
    assert "1食の野菜の合計の目安" in p


def test_copy_meal_estimator_has_the_same_rules():
    """コピーご飯などの栄養推定（写真なし）にも同じ基準を入れること。"""
    p = m.ESTIMATE_NUTRITION_PROMPT
    assert "汁は数えず具だけ" in p
    assert "ミニトマト1個≒10〜15g" in p
    assert "餃子" in p and "迷ったら少なめ" in p
