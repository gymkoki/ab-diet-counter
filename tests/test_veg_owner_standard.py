"""野菜量のオーナー基準（2026-10-13）の回帰テスト。

オーナー指示：
  野菜の摂取量の測定精度をもっと上げてほしい。小鉢1個分なら大体70g、2個分なら140g。
  焼いたり煮たりすると野菜の量が小さくなるため、一般的な肉野菜炒めの場合は、
  かさが減っている分、野菜の摂取量は140g前後くらい（ケースバイケース）。

写真解析・文章入力・訂正（ANALYSIS_PROMPT）と、コピーご飯の栄養推定（ESTIMATE_NUTRITION_PROMPT）の
両方に同じ基準が入っていること、古い目安（小鉢40〜60g）が残って矛盾していないことを固定する。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
import app as m  # noqa: E402

A = m.ANALYSIS_PROMPT
E = m.ESTIMATE_NUTRITION_PROMPT


def test_kobachi_is_70g_each():
    assert "1個 ≒ 70g で数える。2個なら ≒ 140g" in A
    assert "サラダの小鉢など）は1個≒70g、2個≒140g" in E


def test_cooked_vegetables_are_not_undercounted():
    assert "かさが減って見た目が小さくなる" in A
    assert "一般的な肉野菜炒め1人前" in A and "野菜 ≒ 140g 前後" in A
    assert "肉が入っていても野菜の量は減らさない" in A
    assert "かさが減って小さく見えるので少なく数えない" in E and "肉野菜炒め1人前≒140g前後" in E


def test_non_vegetable_kobachi_are_excluded():
    """小鉢でも野菜が主役でないもの（冷奴・卵焼きなど）は70gにしない。"""
    assert "冷奴・卵焼き" in A


def test_meal_totals_match_the_new_standard():
    """定食の目安が小鉢70gを前提にした数字になっている（古い「60〜120g」で打ち消さない）。"""
    assert "小鉢70g＋味噌汁の具30〜50g ≒ 100〜120g" in A
    assert "小鉢140g＋味噌汁の具30〜50g ≒ 170〜190g" in A
    assert "主菜＋小鉢＋汁物の家庭の定食：野菜の合計は 60〜120g が普通" not in A
    assert "野菜の小鉢が2個以上" in A


def test_old_kobachi_40_60_is_gone():
    for p in (A, E):
        assert "小鉢のサラダ≒40〜60g" not in p and "サラダ小鉢≒40〜60g" not in p


def test_safety_net_does_not_cap_kobachi_or_stir_fry():
    """上限をかける安全網が、小鉢70g・肉野菜炒め140gを削らないこと。"""
    foods = [
        {"name": "ほうれん草のおひたし", "veg_g": 70},
        {"name": "ひじきの煮物", "veg_g": 70},
        {"name": "肉野菜炒め", "veg_g": 140},
        {"name": "きんぴらごぼう（小鉢）", "veg_g": 70},
    ]
    m._sanitize_veg(foods)
    assert [f["veg_g"] for f in foods] == [70, 70, 140, 70]
    assert not any("veg_adjusted_from" in f for f in foods)
