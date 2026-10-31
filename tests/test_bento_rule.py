# お弁当のBカウントの回帰テスト（オーナー指摘 2026-10-31）。
#
# 「そぼろ弁当（ご飯・甘辛そぼろ・炒り卵・梅干し・ミニコロッケ2個・ブロッコリー・煮物）がB3なのはおかしい。
#  白米でB1、他の食材でB0.5〜1くらいでB1.5〜2くらい」。
# 原因：「炒め物・揚げ物には最低でも調理油 大さじ1（B0.5）」のルールを、弁当の小さなおかず1品ずつに当てはめて
#   油だけで B1〜1.5 増えていた。1皿ぶんの主菜の目安なので、弁当には別のルールを置いた。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

P = m.ANALYSIS_PROMPT


def test_bento_section_exists():
    assert "お弁当（弁当箱）の判定ルール" in P


def test_cooking_oil_once_per_bento():
    """調理油は弁当全体で1項目。おかずごとに大さじ1を立てない。"""
    assert "調理油はお弁当全体で1項目だけ" in P
    assert "おかずごとに「調理油 大さじ1」を別々に立てない" in P
    # 1皿ぶんの主菜向けの「最低でも大さじ1」は、弁当には当てはめないと明記
    i = P.index("最低でも「大さじ1（135kcal）＝B0.5カウント」")
    assert "お弁当の小さなおかず・小鉢には当てはめない" in P[i:i + 600]


def test_small_bento_fried_items_include_oil():
    assert "油込みのkcalで1項目" in P and "「揚げ油（吸収油）」を別に立てない" in P
    assert "1人前の大きな揚げ物が主菜なら、通常の揚げ物ルールで数える" in P


def test_owner_example_and_typical_range():
    assert "そぼろ弁当" in P and "＝ **B2**" in P
    assert "B1.5〜2.5 が目安" in P


def test_example_is_consistent_with_step2():
    """例の各項目のBが、STEP2（kcal→B）と食い違っていないこと（AIが例と規則の板挟みにならないように）。"""
    for kcal, b in ((130, 0.5), (90, 0), (180, 0.5), (300, 1)):
        assert m._b_from_kcal(kcal) == b
