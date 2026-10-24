# バナナのBカウントを本数でそろえる回帰テスト（会員の声 2026-10-24）。
#
# 「バナナ4分の1が、Bカウント0の時と0.5の時があるので統一していただきたい」。原因は2つ：
#   ①AIへの指示に「1本→B0.5」「半分→B0」しか無く、「最低B0.5」の一文に引っぱられて1/4本でもB0.5にすることがあった
#   ②量ボタンがBをそのまま掛けて四捨五入していたため、B0.5の0.5倍（0.25）が切り上げでB0.5のまま残った
# 対策：早見表（1本未満→B0／1〜1.5本→B0.5／2本以上→B1）を AI・サーバーの安全網・量ボタンで共通にした。
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _food(name, b, kcal, amount="", reason=""):
    return {"name": name, "category": "B", "kcal_per_serving": kcal, "b_count": b,
            "protein_g": 1, "veg_g": 0, "amount": amount, "reason": reason}


# ── サーバーの安全網 ───────────────────────────────────────────
@pytest.mark.parametrize("name,amount,ai_b,kcal,expected", [
    ("バナナ（1/4本）", "", 0.5, 23, 0),          # 会員の例：B0.5 と出ても B0 にそろう
    ("バナナ（1/4本）", "", 0, 23, 0),
    ("バナナ", "4分の1本", 0.5, 23, 0),
    ("バナナ 半分", "", 0.5, 45, 0),
    ("バナナ", "1本", 0, 90, 0.5),                # 120kcal未満でも1本なら B0.5
    ("完熟バナナ", "大きめ1本", 0.5, 120, 0.5),
    ("バナナ 1.5本", "", 1, 140, 0.5),
    ("バナナ2本", "", 0.5, 190, 1),
    ("バナナ", "少量", 0.5, 25, 0),                # 本数が書いていなければ kcal から（≒1/4本）
])
def test_banana_b_is_decided_by_pieces(name, amount, ai_b, kcal, expected):
    foods = [_food(name, ai_b, kcal, amount)]
    r = m.recompute_totals({"foods": foods})
    assert foods[0]["b_count"] == expected
    assert r["total_b_count"] == expected


def test_adjustment_is_recorded():
    foods = [_food("バナナ（1/4本）", 0.5, 23)]
    m.recompute_totals({"foods": foods})
    assert foods[0]["b_adjusted_from"] == 0.5


@pytest.mark.parametrize("name", ["チョコバナナ", "バナナケーキ", "バナナジュース", "バナナとヨーグルト",
                                  "バナナマフィン", "ヨーグルト（バナナ入り）"])
def test_dishes_with_banana_are_left_alone(name):
    foods = [_food(name, 0.5, 60)]
    m.recompute_totals({"foods": foods})
    assert foods[0]["b_count"] == 0.5


def test_member_correction_is_respected():
    foods = [_food("バナナ（1/4本）", 0.5, 23, reason="訂正反映：本人の申告でB0.5")]
    m.recompute_totals({"foods": foods})
    assert foods[0]["b_count"] == 0.5


def test_prompt_has_the_chart():
    p = m.ANALYSIS_PROMPT
    assert "バナナの本数の早見表" in p
    assert "4分の1本・3分の1本・半分（1本未満）→ 【B0】" in p
    assert "「最低B0.5」は1本以上のときだけ" in p
    # 1本＝B0.5 の原則はそのまま
    assert "1本食べたら必ず【B0.5】" in p


# ── アプリの量ボタン（JS）──────────────────────────────────────
def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def _run_js(expr_cases):
    src = re.search(r"function bFromKcal\(kcal\) \{.*?\nfunction scaledBCount\(food, base, factor\) \{.*?\n\}",
                    _html(), re.S).group(0)
    harness = f"""{src}
const cases = {json.dumps(expr_cases, ensure_ascii=False)};
console.log(JSON.stringify(cases.map(c => c.kind === 'scale'
  ? scaledBCount(c.food, c.base, c.factor)
  : bananaB(bananaPieces(c.text, c.kcal), c.kcal))));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(harness)
        path = tmp.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)
    finally:
        os.unlink(path)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")


def _scale(name, b, kcal, amount, factor, reason=""):
    return {"kind": "scale", "food": {"name": name, "reason": reason},
            "base": {"b_count": b, "kcal_per_serving": kcal, "amount": amount}, "factor": factor}


@needs_node
def test_quantity_buttons_follow_the_same_rules():
    cases = [
        _scale("バナナ", 0.5, 95, "1本", 0.5),          # 半分 → B0（以前は B0.5 のまま）
        _scale("バナナ", 0.5, 95, "1本", 1.5),          # 1.5本 → B0.5（以前は B1）
        _scale("バナナ", 0.5, 95, "1本", 2),            # 2本 → B1
        _scale("バナナ（1/2本）", 0, 45, "", 2),        # 半分×2＝1本 → B0.5
        _scale("白米（半膳）", 0.5, 125, "75g", 0.5),   # 62kcal → B0（以前は B0.5 のまま）
        _scale("白米", 1, 250, "150g", 0.5),            # 125kcal → B0.5
        _scale("白米", 1, 250, "150g", 2),              # 500kcal → B2
        _scale("ごま油（例外で最低B0.5）", 0.5, 100, "", 0.5),   # 例外の項目は切り捨て → B0
        _scale("ごま油（例外で最低B0.5）", 0.5, 100, "", 2),     # 増やすときは掛け算 → B1
    ]
    assert _run_js(cases) == [0, 0.5, 1, 0.5, 0, 0.5, 2, 0, 1]


@needs_node
def test_app_and_server_agree_on_bananas():
    texts = [("バナナ（1/4本）", 23), ("バナナ 半分", 45), ("バナナ 1本", 90), ("バナナ 1.5本", 140),
             ("バナナ2本", 190), ("バナナ 3本", 290), ("バナナ 少量", 25), ("バナナ ４分の１本", 23)]
    js = _run_js([{"kind": "plain", "text": t, "kcal": k} for t, k in texts])
    py = [m._banana_b(m._banana_pieces(t, k), k) for t, k in texts]
    assert js == py


@needs_node
def test_app_banana_detection_matches_server():
    names = ["バナナ（1/4本）", "完熟バナナ 1本", "チョコバナナ", "バナナケーキ", "バナナとヨーグルト", "バナナジュース"]
    src = re.search(r"const BANANA_ONLY_REST_RE = .*?\nfunction isBananaOnly\(name\) \{.*?\n\}", _html(), re.S).group(0)
    harness = f"{src}\nconsole.log(JSON.stringify({json.dumps(names, ensure_ascii=False)}.map(isBananaOnly)));"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(harness)
        path = tmp.name
    try:
        out = json.loads(subprocess.run(["node", path], capture_output=True, text=True).stdout)
    finally:
        os.unlink(path)
    assert out == [m._is_banana_only(n) for n in names]
