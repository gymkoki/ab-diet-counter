# バナナのBカウントの回帰テスト。
# 経緯（会員の声 2026-10-21）：「バナナ4分の1が、Bカウント0の時と0.5の時がある」。
# 「バナナ1本で最低B0.5」の特例を、AIが1本未満にも当てることがあった。
# 対策：1本未満は必ずB0、1本以上は最低B0.5 をサーバー側（recompute_totals）で確定させる。

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402


def _b(food):
    res = m.recompute_totals({"foods": [dict(food)]})
    return res["foods"][0]["b_count"], res["total_b_count"]


@pytest.mark.parametrize("food", [
    {"name": "バナナ（1/4本）", "kcal_per_serving": 25, "b_count": 0.5},
    {"name": "バナナ", "amount": "1/4本・約25kcal", "kcal_per_serving": 25, "b_count": 0.5},
    {"name": "バナナ 4分の1本", "kcal_per_serving": 25, "b_count": 0.5},
    {"name": "バナナ ¼本", "kcal_per_serving": 25, "b_count": 0.5},
    {"name": "バナナ（半分）", "kcal_per_serving": 45, "b_count": 0.5},
    {"name": "バナナ", "amount": "少量", "fruit_g": 25, "kcal_per_serving": 23, "b_count": 0.5},
    {"name": "バナナ", "kcal_per_serving": 25, "b_count": 0.5},
])
def test_quarter_banana_is_always_zero(food):
    assert _b(food) == (0, 0)


@pytest.mark.parametrize("food", [
    {"name": "バナナ", "amount": "1本", "kcal_per_serving": 90, "b_count": 0},
    {"name": "バナナ（1本）", "kcal_per_serving": 95, "b_count": 0},
    {"name": "バナナ", "amount": "小さめ", "fruit_g": 85, "kcal_per_serving": 75, "b_count": 0},
])
def test_one_banana_is_at_least_half(food):
    assert _b(food) == (0.5, 0.5)


def test_two_bananas_keep_ai_value():
    assert _b({"name": "バナナ 2本", "kcal_per_serving": 200, "b_count": 1}) == (1, 1)


def test_same_quarter_banana_gets_same_count_every_time():
    """AIの答えが0でも0.5でも、1/4本なら結果は同じになること（会員の指摘そのもの）。"""
    a = _b({"name": "バナナ（1/4本）", "kcal_per_serving": 25, "b_count": 0})
    b = _b({"name": "バナナ（1/4本）", "kcal_per_serving": 25, "b_count": 0.5})
    assert a == b == (0, 0)


def test_adjusted_item_keeps_original_and_reason():
    res = m.recompute_totals({"foods": [{"name": "バナナ 1/4本", "kcal_per_serving": 25, "b_count": 0.5}]})
    f = res["foods"][0]
    assert f["b_adjusted_from"] == 0.5
    assert "1本未満" in f["reason"]


@pytest.mark.parametrize("name", [
    "チョコバナナ", "バナナケーキ", "バナナスムージー", "バナナジュース", "チョコバナナアイス",
    "バナナマフィン", "ヨーグルト（バナナ入り）バナナヨーグルト", "バナナオムレツ",
])
def test_processed_banana_items_are_untouched(name):
    assert _b({"name": name, "kcal_per_serving": 60, "b_count": 0.5}) == (0.5, 0.5)


def test_other_foods_untouched():
    assert _b({"name": "りんご 1/4個", "kcal_per_serving": 30, "b_count": 0.5}) == (0.5, 0.5)


def test_prompt_states_under_one_banana_is_zero():
    assert "1本に満たない量 → 必ず【B0】" in m.ANALYSIS_PROMPT
    assert "1本未満（半分・1/4本など）はこの例外の対象外で、必ずB0" in m.ANALYSIS_PROMPT
