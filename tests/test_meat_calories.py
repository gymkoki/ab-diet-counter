# 肉のカロリー基準（日本食品標準成分表ベース）の回帰テスト。
# 背景（2026-08 オーナー指摘）：
#   肉うどんの豚バラ60gが「約166kcal（100gあたり277kcal）→B0.5」と計算された。
#   豚バラ（脂身つき・生）は成分表で100gあたり366kcalであり、60g＝約220kcal＝B1が正しい。
#   原因は、プロンプトが「豚バラ・牛バラ（約250〜400kcal）」と幅の広い範囲指定で、
#   AIが下限寄りの値を選んでいたこと。部位別の具体値に置き換えた。
# 同じ過小評価が再発しないよう、基準値がプロンプトに載っていることを固定する。

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_prompt_has_meat_calorie_reference_table():
    """成分表ベースの部位別カロリー基準表が入っていること。"""
    src = _read(APP)
    assert "肉の部位別カロリー基準表" in src
    assert "日本食品標準成分表" in src


def test_pork_belly_is_366_not_underestimated():
    """豚バラが366kcal/100gで固定され、旧来の曖昧な範囲指定が残っていないこと。"""
    src = _read(APP)
    assert "豚バラ（100gあたり約366kcal）" in src, "豚バラのB食材例が366kcalになっていません"
    assert "366kcal" in src
    # 下限寄りの値を選ばせていた旧範囲指定が消えていること
    assert "豚バラ・牛バラ・牛カルビ（100gあたり約250〜400kcal）" not in src, \
        "豚バラが幅の広い範囲指定に戻っています（過小評価の原因）"
    # 低い値を使わない明示的な禁止
    assert "250〜280kcal等の低い値を使わないこと" in src


def test_pork_belly_60g_example_is_b1():
    """豚バラ60g＝約220kcal＝B1 の計算例が入っていること（オーナー指摘のケース）。"""
    src = _read(APP)
    assert "60g × 366 ÷ 100 ≒ 220kcal → 200kcal超 → B1" in src
    assert "【豚バラ肉（B食材：100gあたり約366kcal → 200kcal超のためB食材）】" in src
    assert "・薄切り5〜6枚（約60g、約220kcal）→ 200kcal超 → 1カウント" in src


def test_chashu_value_does_not_leak_into_raw_pork_belly():
    """チャーシュー専用の210kcalが、生の豚バラ（366kcal）に流用されないよう明記されていること。"""
    src = _read(APP)
    assert "生の豚バラ肉そのものには使わず" in src
    # チャーシューの見出しから「豚バラ」の紛らわしいラベルが外れていること
    assert "※チャーシュー（煮豚・焼き豚）は100gあたり約210kcal前後" in src


def test_other_cuts_have_specific_values():
    """主要な部位に具体値が入っていること（範囲だけで済ませない）。"""
    src = _read(APP)
    for kw in [
        "豚ロース ＝ 248kcal",
        "豚もも ＝ 171kcal",
        "牛バラ・カルビ ＝ 輸入 338kcal ／ 和牛 472kcal",
        "鶏もも（皮つき）＝ 204kcal",
        "鶏ささみ ＝ 98kcal",
    ]:
        assert kw in src, f"部位別の基準値『{kw}』が見当たりません"


def test_fatty_cut_rule_prefers_higher_value():
    """脂身つき／なしで迷ったら高い方を使う指示が入っていること（過小評価の防止）。"""
    src = _read(APP)
    assert "脂身つき（高い方）" in src
    assert "低い方を選ぶ（過小評価）のは禁止" in src
