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


def test_bacon_is_400_not_185():
    """ベーコンが成分表の400kcal/100gで計算されること。
    （旧設定の185kcalはショルダーベーコンの値で、通常のベーコンには低すぎた）"""
    src = _read(APP)
    assert "ベーコン（ばらベーコン）＝ 400kcal" in src
    assert "ベーコン（100gあたり約400kcal）" in src, "ベーコンがB食材例に載っていません"
    # 旧値をそのまま使う記述が残っていないこと（誤用禁止の注意書きは可）
    assert "ベーコンは100gあたり約185kcal（A食材寄り）" not in src, \
        "ベーコンが旧値185kcal（A食材寄り）に戻っています"
    assert "通常のベーコンに使ってはいけない" in src


def test_bacon_amount_examples_recalculated():
    """ベーコンの量別の判定例が400kcal/100gで再計算されていること。"""
    src = _read(APP)
    for kw in [
        "ベーコン1枚（約10g・約40kcal）→ 120kcal未満 → 0カウント",
        "ベーコン3枚（約30g・約120kcal）→ 120〜200kcal → 0.5カウント",
        "ベーコン1人前（約60g・約240kcal）→ 200kcal超 → 1カウント",
        "ベーコン大量（約120g・約480kcal）→ 400kcal超 → 2カウント",
    ]:
        assert kw in src, f"ベーコンの判定例『{kw}』が見当たりません"
    # 薄切り1枚の重量目安も新しいカロリーに合わせてあること
    assert "ベーコン薄切り1枚 ≒ 8〜10g（約32〜40kcal）" in src


def test_processed_meat_values():
    """加工肉（ウインナー・ハム）の基準値が入っていること。"""
    src = _read(APP)
    assert "ウインナーソーセージ ＝ 319kcal" in src
    assert "生ハム ＝ 243kcal" in src
    assert "ロースハム ＝ 211kcal" in src


def test_fatty_cut_rule_prefers_higher_value():
    """脂身つき／なしで迷ったら高い方を使う指示が入っていること（過小評価の防止）。"""
    src = _read(APP)
    assert "脂身つき（高い方）" in src
    assert "低い方を選ぶ（過小評価）のは禁止" in src
