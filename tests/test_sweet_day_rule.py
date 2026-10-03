# お菓子・スイーツ・ジュースの「1日累積」特別ルールの回帰テスト。
#
# オーナー指示 2026-10-01：
#   お菓子は1回あたり120kcal未満ならB0回として計算しているが、
#   お菓子・スイーツ・ジュースは1日の累積で
#     120〜200kcal → B0.5回 ／ 200kcal超 → B1回
#   を追加する。累積でB0.5以上になったら画面に「特別ルール適用！」を表示する。
#   （以前は「200kcal超で+1」だけだった。ジュースも対象に加えた）
#
# 固定する不変条件：
#   ①しきい値（120〜200で+0.5、200超で+1）がアプリ側とサーバー側で一致している
#   ②1回で既にB0.5以上が付いたものは累積に入れない（二重計上しない）
#   ③サーバーの1日集計（_summarize_day_meals）にも累積分が入る（画面と数字がずれない）
#   ④AIの判定ルールでジュース（甘い飲み物）も sweet_kcal の対象になっている
#   ⑤画面に「特別ルール適用！」の表示がある／アップデートお知らせで伝えている

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")

import app as m  # noqa: E402

INDEX = os.path.join(ROOT, "templates", "index.html")


def _html():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def _food(name, sweet_kcal, b_count, kcal=None):
    return {"name": name, "sweet_kcal": sweet_kcal, "b_count": b_count,
            "kcal_per_serving": kcal if kcal is not None else sweet_kcal}


# ── ① サーバー側のしきい値 ─────────────────────────────
@pytest.mark.parametrize("kcal,bonus", [
    (0, 0), (119, 0), (120, 0.5), (160, 0.5), (200, 0.5), (201, 1), (450, 1),
])
def test_server_bonus_thresholds(kcal, bonus):
    assert m._day_sweet_bonus_b(kcal) == bonus


# ── ② 二重計上しない ─────────────────────────────────
def test_only_b0_sweets_accumulate():
    foods = [
        _food("チョコ1かけら", 60, 0),
        _food("オレンジジュース200ml", 84, 0),
        _food("コーラ350ml", 150, 0.5),     # 1回でB0.5が付いている → 累積しない
        _food("白米", 0, 1, kcal=250),      # お菓子でない
    ]
    assert m._day_sweet_b0_kcal(foods) == 144


# ── ③ サーバーの1日集計にも累積分が入る ─────────────────────
def _payload(*foods_per_item):
    items = []
    for foods in foods_per_item:
        tb = sum(f["b_count"] for f in foods)
        items.append({"result": {"foods": list(foods), "total_b_count": tb}})
    return json.dumps({"food": {"items": items}})


def test_day_summary_adds_half_for_120_to_200():
    p = _payload(
        [_food("白米", 0, 1, kcal=250)],
        [_food("クッキー1枚", 50, 0)],
        [_food("オレンジジュース200ml", 84, 0)],   # 合計134kcal → +0.5
    )
    assert m._summarize_day_meals(p)["b_count"] == 1.5


def test_day_summary_adds_one_over_200():
    p = _payload(
        [_food("チョコ", 90, 0)],
        [_food("グミ", 70, 0)],
        [_food("スポーツドリンク500ml", 105, 0)],   # 合計265kcal → +1
    )
    assert m._summarize_day_meals(p)["b_count"] == 1.0


def test_day_summary_no_bonus_under_120():
    p = _payload([_food("飴2粒", 40, 0)], [_food("せんべい1枚", 50, 0)])   # 90kcal
    assert m._summarize_day_meals(p)["b_count"] == 0.0


def test_day_summary_does_not_double_count():
    """1回でB1が付いたケーキは累積に入れない。B0の飴だけでは120に届かないので追加なし。"""
    p = _payload([_food("ショートケーキ", 330, 1)], [_food("飴", 40, 0)])
    assert m._summarize_day_meals(p)["b_count"] == 1.0


# ── ① アプリ側（JS）も同じしきい値で動く ─────────────────────
@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_frontend_bonus_matches_server():
    html = _html()
    start = html.index("const SWEET_DAY_HALF_KCAL")
    end = html.index("function daySweetBonusB(items)")
    end = html.index("}", end) + 1
    js = html[start:end] + """
const cases = [0, 119, 120, 160, 200, 201, 450];
const out = cases.map(k => sweetBonusFromKcal(k));
const items = [
  { result: { foods: [ { sweet_kcal: 60, b_count: 0 }, { sweet_kcal: 84, b_count: 0 } ] } },
  { result: { foods: [ { sweet_kcal: 150, b_count: 0.5 } ] } },
];
console.log(JSON.stringify({ out, kcal: daySweetB0Kcal(items), bonus: daySweetBonusB(items) }));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(js)
        path = tmp.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        d = json.loads(proc.stdout)
    finally:
        os.unlink(path)
    assert d["out"] == [m._day_sweet_bonus_b(k) for k in (0, 119, 120, 160, 200, 201, 450)], \
        "アプリとサーバーでしきい値がずれている"
    assert d["kcal"] == 144 and d["bonus"] == 0.5


def test_thresholds_are_the_same_constants():
    html = _html()
    assert f"const SWEET_DAY_HALF_KCAL = {m.SWEET_DAY_HALF_KCAL};" in html
    assert f"const SWEET_DAY_FULL_KCAL = {m.SWEET_DAY_FULL_KCAL};" in html
    assert "SWEET_DAY_THRESHOLD_KCAL" not in html, "旧ルール（200kcal超で+1だけ）の定数が残っている"


# ── ④ AIの判定ルール：ジュースも対象 ─────────────────────────
def test_prompt_includes_juice_as_sweet():
    P = m.ANALYSIS_PROMPT
    assert "お菓子・スイーツ・スナック菓子・ジュース（甘い飲み物）なら" in P
    assert "炭酸飲料（コーラ・サイダー等）" in P and "スポーツドリンク" in P
    assert "1日の合計が120〜200kcalならB0.5、200kcalを超えたらB1を追加する" in P
    # 甘くない飲み物とお酒は対象外
    assert "甘くない飲み物（水・お茶・ブラックコーヒー" in P
    assert "アルコール飲料" in P


def test_schema_sweet_kcal_mentions_juice():
    P = m.ANALYSIS_PROMPT
    assert P.count("またはジュース（果汁ジュース・炭酸飲料・スポーツドリンク・加糖の缶コーヒー等の甘い飲み物）") >= 4


# ── ⑤ 画面表示とお知らせ ─────────────────────────────────
def test_special_rule_banner_is_shown():
    html = _html()
    bar = re.search(r'<div id="sweet-bonus-bar".*?</div>', html, re.S).group(0)
    assert "特別ルール適用！" in bar
    assert "お菓子・スイーツ・ジュース 合計" in bar
    assert 'id="sweet-bonus-num"' in bar, "+0.5／+1 を出し分ける欄が無い"


def test_update_notice_announces_the_rule():
    """特別ルールの中身（120〜200kcal→＋0.5／200kcal超→＋1）が会員に見えること。

    以前は告知した「最新アップデート No.35」の本文を直接検査していたが、
    お知らせは新しい内容へ入れ替わっていくため、そこに固定すると
    お知らせを更新するたびにこのテストが落ちてしまう（test_weight_gate.py と同じ理由）。
    検査対象を、ルールが適用されたときに常に出る「特別ルール適用！」の表示へ移した。"""
    html = _html()
    assert "特別ルール適用！" in html
    assert "120〜200kcal → ＋0.5" in html, "＋0.5 になる範囲が画面に出ない"
    assert "200kcal超 → ＋1" in html, "＋1 になる範囲が画面に出ない"