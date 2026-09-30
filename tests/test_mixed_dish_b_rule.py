# 「料理の中のB食材は、そのB食材だけのkcalで判定する」ルールの回帰テスト。
#
# 背景（2026-09-30 オーナー指摘）：
#   豚汁が「豚バラ30g×366/100≒110kcal＋野菜・汁分≒160kcal→120〜200→B0.5」と判定された。
#   豚バラ自体は110kcal（120kcal未満）なのに、A食材の野菜・こんにゃく・味噌汁のkcalを
#   足したせいでB0.5に上がっていた。オーナーの判断は「豚肉自体が110kcalならB0」。
#
# 対策：料理の中にB食材とA食材が混ざっているときは別項目に分け、
#   B食材の項目はB食材だけのkcalで判定する（A食材・汁のkcalは足さない）。
#   A食材の項目にも実際のkcalは入れる（1日の摂取カロリーの合計に使うため）。
#
# 写真解析(/analyze)・訂正(/reanalyze)・文章入力(/analyze-text)は
# すべて ANALYSIS_PROMPT を使うので、ここで固定すれば3つとも守られる。

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")

import app as m  # noqa: E402

P = m.ANALYSIS_PROMPT


def test_rule_exists_b_food_judged_by_its_own_kcal():
    assert "料理の中のB食材は「そのB食材だけのkcal」で判定する" in P
    assert "A食材や、味噌・だし等の汁のkcalは絶対に足さない" in P


def test_tonjiru_bad_example_is_explicitly_forbidden():
    """オーナーが指摘した誤り方そのものを悪い例として載せている。"""
    assert "❌ 悪い例：豚汁を1項目にまとめて" in P
    assert "野菜・汁分≒160kcal→120〜200→B0.5" in P


def test_tonjiru_correct_example_is_b0():
    """豚バラ30g≒110kcalならB0、というオーナーの判断が正しい例として入っている。"""
    assert "豚バラ30g×366/100≒110kcal→120kcal未満→B0" in P
    assert "豚汁全体で B0" in P


def test_a_part_keeps_real_kcal_for_daily_total():
    """A食材の項目を0kcalにすると1日の摂取カロリーが減ってしまうので、実kcalを入れる。"""
    assert "kcal_per_serving には実際のkcalを入れる（1日の摂取カロリーの合計に使うため、0にしない）" in P
    assert "摂取カロリーは合計160kcalのまま正しく残る" in P


def test_large_b_portion_still_counts():
    """B食材が多ければ、B食材だけで通常どおりカウントする（B0に寄せすぎない）。"""
    assert "豚バラ60g≒220kcalなら、豚バラの項目だけで200kcal超→B1" in P


def test_split_items_use_schema_category_values():
    """例のcategoryは出力形式どおり "B"／"A"（「B食材」等の表記だと画面の判定がずれる）。
    また、B食材がB0になっても category をAに変えない（豚バラはB食材のまま）。"""
    assert "name「豚汁の豚バラ」／category「B」" in P
    assert "category「A」／kcal_per_serving 50" in P
    assert "120kcal未満でB0になってもAに変えない" in P


def test_pork_belly_example_points_to_the_rule():
    assert "Bカウントの判定は豚バラ部分のkcalだけで行う" in P


def test_all_three_entry_points_share_the_prompt():
    """写真・訂正・文章の3経路が同じプロンプトを使っていること（片方だけ古いルールにならない）。"""
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"),
              encoding="utf-8") as f:
        src = f.read()
    assert src.count('"text": ANALYSIS_PROMPT,') >= 3
