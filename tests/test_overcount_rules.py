# Bカウントの数えすぎを防ぐルールの回帰テスト（オーナー指示 2026-10-31）。
#
# お弁当がB3と出た件（tests/test_bento_rule.py）のあと「他のメニューも同様に過大になってないか確認して」。
# 解析の指示を見直して、同じ仕組みで増えていたところを直した：
#   ①定食・家庭の食事の小鉢・副菜にも「調理油 大さじ1」を1品ずつ立てていた → 食事全体で1項目に
#   ②マヨネーズ・ドレッシングを油そのもの（大さじ1＝135kcal）で計算していた → 実際は約60〜80kcal
#   ③「多めに見積もる」を1つの項目に何度も重ねていた → 主食・主菜で1回だけ
#   ④最後に合計を料理ごとの目安と見比べ、数えすぎを見直す（数字を合わせるために削らない）
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

P = m.ANALYSIS_PROMPT


def test_side_dish_oil_is_one_item_per_meal():
    assert "定食・小鉢・副菜が多い食事の判定ルール" in P
    assert "食事全体で「調理油（小鉢・副菜）」1項目にまとめる" in P
    assert "小鉢ごとに「調理油 大さじ1」を別々に立てない" in P
    # 主菜向けの「最低でも大さじ1」から、小鉢のルールへ案内している
    i = P.index("最低でも「大さじ1（135kcal）＝B0.5カウント」")
    assert "定食・小鉢・副菜が多い食事の判定ルール" in P[i:i + 600]


def test_strict_section_limits_stir_fry_oil_to_main_dish():
    i = P.index("量が不確かなときは「厳しめ（多め）」に見積もる")
    s = P[i:]
    assert "炒め物（1人前の主菜）：最低でも大さじ1（約135kcal）" in s
    assert "最低でも大さじ1（約110kcal）" not in P, "110kcal は B0 の段なのに B0.5 と書いてあった"


def test_dressing_and_mayo_are_lighter_than_oil():
    assert "マヨネーズ・ドレッシングは油より軽い" in P
    assert "マヨネーズ 大さじ1（約12g）≒ 約80kcal" in P
    assert "油入りのドレッシング（ごま・シーザー・フレンチ・イタリアン等）大さじ1（約15g）≒ 約60kcal" in P
    assert "ドレッシング（大さじ1.5程度、約165kcal）→ 0.5カウント" not in P
    # 油の換算表の対象からマヨネーズ・ドレッシングを外した
    assert "ラード・マヨネーズ・油ベースのタレやドレッシングすべてに適用する" not in P


def test_upward_estimates_are_not_stacked():
    assert "主食・主菜の量が本当に読み取れないときに、その項目で1回だけ" in P
    assert "小鉢・副菜・付け合わせ・お弁当の小さなおかずには使わない" in P
    # 過小評価を防ぐ方針そのものは残す（tests/test_analysis_flow.py と同じ約束）
    assert "厳しめ" in P and "過小評価" in P


def test_final_total_review():
    i = P.index("■ 合計の見直し")
    s = P[i:i + 1200]
    for kw in ("丼もの・カレーライス・パスタ・チャーハン（1人前）… B2〜3",
               "普通の定食・家庭の夕食（ご飯並盛＋主菜＋小鉢＋汁物）… B2〜3",
               "同じ油を2回以上数えていないか",
               "目安を超えたままでよい",
               "目安に合わせるために量を削る・数字を合わせることはしない"):
        assert kw in s, kw


def test_examples_are_consistent_with_step2():
    """新しい例の数字が STEP2（kcal→B）と食い違わないこと。"""
    for kcal, b in ((110, 0), (90, 0), (180, 0.5), (135, 0.5), (60, 0), (80, 0)):
        assert m._b_from_kcal(kcal) == b
