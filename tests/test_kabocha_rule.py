# かぼちゃのA/Bの判定をそろえる回帰テスト（オーナー指摘 2026-11-11）。
#
# 「かぼちゃがA食材になったりB食材になったりします。改善して」。
# 原因：AIへの指示では「野菜全般＝A」「芋類＝B」のどちらにも読めたうえ、小鉢1個が120kcal前後で判定がぶれていた。
# 対策：かぼちゃは必ずB食材。Bは重さ（×0.9kcal/g）の早見表で決める（〜130g→B0／130〜220g→B0.5／220g超→B1）。
#       AIへの指示（ANALYSIS_PROMPT）とサーバーの安全網（_sanitize_kabocha）の両方で同じにする。
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402
from conftest import seed_weight  # noqa: E402


def _food(name, category="A", b=0, kcal=100, amount="", reason=""):
    return {"name": name, "category": category, "kcal_per_serving": kcal, "b_count": b,
            "protein_g": 2, "veg_g": 80, "amount": amount, "reason": reason}


# ── 同じ量なら毎回同じ category・同じB ───────────────────────────
@pytest.mark.parametrize("name,amount,expected_b", [
    ("かぼちゃの煮物", "3切れ", 0),          # 105g → 95kcal → B0
    ("かぼちゃの煮物（2切れ）", "", 0),
    ("かぼちゃの煮物", "小鉢1個", 0),        # 100g → 90kcal → B0
    ("かぼちゃの煮物", "5切れ", 0.5),        # 175g → 158kcal → B0.5
    ("蒸しかぼちゃ", "150g", 0.5),
    ("かぼちゃ", "250g", 1),                 # 225kcal → B1
    ("南瓜の含め煮", "4切れ", 0.5),          # 140g → 126kcal → B0.5
])
@pytest.mark.parametrize("ai_category,ai_b,ai_kcal", [("A", 0, 90), ("B", 0.5, 130), ("B", 0, 110)])
def test_same_amount_gives_same_result(name, amount, expected_b, ai_category, ai_b, ai_kcal):
    """AIが A／B・B0／B0.5 のどれを返しても、安全網で同じ結果にそろう。"""
    foods = [_food(name, ai_category, ai_b, ai_kcal, amount)]
    m._sanitize_kabocha(foods)
    assert foods[0]["category"] == "B"
    assert foods[0]["b_count"] == expected_b


def test_category_fixed_even_without_amount():
    """重さが書いていなくても、A食材にはしない（Bはkcalのまま）。"""
    foods = [_food("かぼちゃの煮物", "A", 0, 95)]
    m._sanitize_kabocha(foods)
    assert foods[0]["category"] == "B" and foods[0]["b_count"] == 0
    assert foods[0]["kcal_per_serving"] == 95


@pytest.mark.parametrize("name", [
    "かぼちゃサラダ", "かぼちゃのポタージュ", "かぼちゃの天ぷら", "かぼちゃコロッケ",
    "かぼちゃプリン", "かぼちゃのそぼろ煮", "かぼちゃ大福", "ほうれん草のおひたし",
])
def test_mixed_dishes_are_left_alone(name):
    foods = [_food(name, "A", 0, 100, "150g")]
    m._sanitize_kabocha(foods)
    assert foods[0]["category"] == "A" and foods[0]["b_count"] == 0


def test_member_correction_is_kept():
    foods = [_food("かぼちゃの煮物", "A", 0, 200, "5切れ", reason="訂正反映：量を少なめに")]
    m._sanitize_kabocha(foods)
    assert foods[0]["category"] == "A" and foods[0]["b_count"] == 0


def test_original_values_are_kept_for_review():
    foods = [_food("かぼちゃの煮物", "A", 0, 120, "5切れ")]
    m._sanitize_kabocha(foods)
    f = foods[0]
    assert f["category_adjusted_from"] == "A" and f["b_adjusted_from"] == 0
    assert f["kcal_adjusted_from"] == 120 and f["kcal_per_serving"] == 158


def test_totals_follow_the_fix():
    res = {"foods": [_food("白米", "B", 1, 250, "150g"), _food("かぼちゃの煮物", "A", 0, 120, "5切れ")],
           "total_b_count": 1}
    m.recompute_totals(res)
    assert res["total_b_count"] == 1.5


# ── 解析の入口を通るとそろって返る ──────────────────────────────
@pytest.fixture
def client(monkeypatch):
    m.app.config["TESTING"] = True
    monkeypatch.setattr(m, "get_client", lambda: object())
    seed_weight("kabocha-user")
    with m.app.test_client() as c:
        yield c


def test_analyze_text_returns_b(client, monkeypatch):
    reply = {"foods": [_food("かぼちゃの煮物", "A", 0, 90, "3切れ")],
             "total_b_count": 0, "total_protein_g": 2, "total_veg_g": 80, "advice": ""}
    monkeypatch.setattr(m, "_create_with_server_tools", lambda *a, **k: None)
    monkeypatch.setattr(m, "parse_ai_result", lambda resp: copy.deepcopy(reply))
    r = client.post("/analyze-text", data={"text": "かぼちゃの煮物3切れ", "user_id": "kabocha-user"})
    assert r.status_code == 200, r.get_json()
    f = r.get_json()["foods"][0]
    assert f["category"] == "B" and f["b_count"] == 0


# ── AIへの指示 ─────────────────────────────────────────────────
def test_prompt_fixes_kabocha_as_b():
    p = m.ANALYSIS_PROMPT
    assert "かぼちゃ【必ずB食材として扱う・A食材にしない】" in p
    assert "〜130g）→ B0" in p and "130〜220g）→ B0.5" in p
