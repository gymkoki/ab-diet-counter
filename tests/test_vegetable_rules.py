# 野菜量(veg_g)の推定ルールの回帰テスト。
# 背景（2026-08 オーナー指摘）：
#   サーモン丼（大根おろし・シラス・刻み葱）に野菜40gが計上され、
#   「明らかに海鮮丼に野菜は入っていない」と指摘された。
#   大根おろしや刻みねぎは数gの薬味であり、野菜を摂ったとは言えない。
#   薬味を野菜として数えると1日の野菜量が水増しされ、採点（野菜25%）も甘くなる。

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_garnish_is_not_counted_as_vegetable():
    """薬味・つまを野菜に数えないルールが入っていること。"""
    src = _read(APP)
    assert "薬味やつまは野菜に数えない（veg_g=0）" in src
    # 代表的な薬味が列挙されていること
    for kw in ["大根おろし（薬味程度）", "刻みねぎ", "大葉", "つま（刺身の千切り大根）", "紅生姜", "刻み海苔"]:
        assert kw in src, f"薬味の例『{kw}』が見当たりません"


def test_seafood_bowl_example_is_zero_veg():
    """オーナー指摘のケース（海鮮丼＝野菜0g）が例として入っていること。"""
    src = _read(APP)
    assert "サーモン丼・海鮮丼（大根おろし・シラス・刻みねぎ）→ 野菜のおかずではないので veg_g=0" in src
    assert "刺身のつま → すべて veg_g=0" in src


def test_veg_sanity_check_rule():
    """野菜のおかずが無いのに野菜量が多い場合の見直しルールが入っていること。"""
    src = _read(APP)
    assert "野菜量の妥当性チェック" in src
    assert "薬味やつまを数えていないか必ず見直すこと" in src
    # 丼・寿司系は0〜10g程度が正しいという基準
    assert "total_veg_g は 0〜10g 程度が正しい" in src


def test_real_vegetable_dishes_still_counted():
    """おかずとして盛られた野菜は従来どおり計上されること（過剰な0化を防ぐ）。"""
    src = _read(APP)
    assert "野菜として計上してよいもの" in src
    # 大量にある場合の例外が残っていること
    assert "おかず1品" in src
    assert "海藻サラダ1皿" in src
    # 既存の分量目安が消えていないこと
    for kw in ["小鉢のサラダ≒40〜60g", "サラダ1皿≒100g", "野菜炒め1人前≒120〜150g"]:
        assert kw in src, f"野菜の分量目安『{kw}』が消えています"
