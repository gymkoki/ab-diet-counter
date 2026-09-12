# 「量の直し」（大盛り・2玉・肉200g）の回帰テスト。
#
# 経緯（オーナー指摘 2026-09）：
#   うどんを2玉頼んでも1玉として数えてしまう。肉を200g食べても100gで数えてしまう。
#   写真からは「2玉か1玉か」「100gか200gか」を判別できないため、AIへの指示を
#   強めるだけでは解決しない（厳しめに見積もる指示はすでに入っている）。
#   そこで①AIに「数えられる単位」を必ず書かせ、②会員がワンタップで倍率を
#   直せるようにした。直しはAIに聞き直さず、その場の掛け算で完結させる
#   （待たせる経路に新しいAI往復を足さない＝CLAUDE.md の方針）。

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def _fn(name):
    html = _html()
    mt = re.search(r"function " + name + r"\(.*?\n\}", html, re.S)
    assert mt, f"{name} が見つかりません"
    return mt.group(0)


# ── ① AIに「数えられる単位」を書かせる ──────────────────────────
def test_prompt_requires_countable_amount():
    """麺は「玉」、ご飯は「杯」、肉魚は「g」で量を書かせること。
    単位が書かれていないと、会員は多い／少ないを判断できず直せない。"""
    p = m.ANALYSIS_PROMPT
    assert "数えられる単位" in p
    for unit in ("玉", "杯", "枚"):
        assert unit in p, f"{unit} の指定がありません"
    assert "2玉" in p, "2玉の書き方の例がありません"
    assert "約200g" in p


def test_prompt_admits_photo_cannot_tell_quantity():
    """写真では量が分からないことを前提として書いてあること
    （AIに「見れば分かるはず」と思わせない）。"""
    assert "原理的に判別できない" in m.ANALYSIS_PROMPT


def test_strict_estimation_rules_are_kept():
    """従来の「迷ったら多め」のルールを消していないこと。"""
    p = m.ANALYSIS_PROMPT
    assert "厳しめ" in p and "過小評価" in p
    assert "主食の大盛り" in p and "肉・魚の大盛り" in p


# ── ② ワンタップで量を直せる ────────────────────────────────────
def test_quantity_buttons_exist():
    """半分・1.5倍・2倍のボタンが用意されていること。"""
    html = _html()
    assert "QTY_FACTORS" in html
    factors = re.search(r"const QTY_FACTORS = \[(.*?)\];", html, re.S).group(1)
    for label in ("半分", "1.5倍", "2倍"):
        assert label in factors, f"「{label}」のボタンがありません"


def test_buttons_are_shown_on_each_food():
    """食材1品ごとにボタンが出ること（うどんだけ2玉、を直せるように）。"""
    html = _html()
    assert "function buildFoodRows(foods, mealId, itemId)" in html
    body = _fn("buildFoodRows")
    assert "setFoodQty(" in body and "qty-btn" in body
    # 呼び出し側が食事と項目を渡していること
    assert "buildFoodRows(item.result.foods, mealId, item.id)" in html


def test_fix_does_not_call_the_ai():
    """量の直しでAIに問い合わせないこと（待ち時間を増やさない）。"""
    body = _fn("setFoodQty")
    for banned in ("fetch(", "reanalyze", "/analyze", "await"):
        assert banned not in body, f"量の直しで {banned} を使っています"


def test_fix_keeps_the_original_estimate():
    """元の見積もりを控えてから掛けること。
    控えないと 2倍→1.5倍 で3倍になってしまう。"""
    body = _fn("setFoodQty")
    assert "qtyBase" in body
    assert "if (!food.qtyBase)" in body, "元の値を1回だけ控える処理がありません"
    # 倍率は必ず「元の値 × 倍率」で計算する
    assert "base.b_count" in body and "base.kcal_per_serving" in body


def test_same_button_cancels():
    """同じボタンをもう一度押すと元に戻ること。"""
    body = _fn("setFoodQty")
    assert "(food.qtyFactor === factor) ? 1 : factor" in body


def test_totals_are_recalculated():
    """直したあと、その食事の合計と今日の合計に反映されること。"""
    body = _fn("setFoodQty")
    for needed in ("recalcResultTotals", "updateDailyTotal()", "saveState()"):
        assert needed in body, f"{needed} を呼んでいません"


def test_recalc_matches_the_server_rule():
    """合計の計算がサーバー(recompute_totals)と同じであること。
    ずれると画面とダッシュボードで数字が食い違う。"""
    body = _fn("recalcResultTotals")
    assert "Math.round(sum('b_count') * 2) / 2" in body, "Bカウントの0.5刻みが違う"
    for key in ("protein_g", "veg_g", "fruit_g", "sweet_kcal"):
        assert key in body, f"{key} の合計を出していません"


def test_bcount_stays_in_half_steps():
    """直したあともBカウントが0.5刻みであること（0.75などにしない）。"""
    body = _fn("setFoodQty")
    assert "Math.round((Number(base.b_count) || 0) * next * 2) / 2" in body


def test_amount_shows_that_it_was_fixed():
    """直したことが画面に残ること（あとで見ても分かるように）。"""
    body = _fn("setFoodQty")
    assert "×${next}" in body
    html = _html()
    assert "に修正済み" in html
