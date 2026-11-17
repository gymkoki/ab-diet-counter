# 鶏むね肉・ささみが B食材として数えられる回帰テスト（オーナー指摘 2026-11-17：「鳥むね肉がB食材カウントされます なぜ？」）。
#
# 原因は2つ：
#   ①量ボタン（1.5倍・2倍）が A食材でも kcal から B を付け直していた（鶏むね100g≒105kcal ×2 → 210kcal → B1）
#   ②AIがまれに量の多い鶏むね肉に STEP2 を当てたり B に分類したりしても、サーバーに安全網が無かった
# ルールは「鶏むね肉（皮なし）・ささみ＝A食材」「A食材はどれだけでも0カウント」。
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


def _food(name, b, cat="B", kcal=300, reason=""):
    return {"name": name, "category": cat, "kcal_per_serving": kcal, "b_count": b,
            "protein_g": 60, "veg_g": 0, "amount": "", "reason": reason}


# ── サーバーの安全網 ───────────────────────────────────────────
@pytest.mark.parametrize("name", [
    "鶏むね肉", "鳥むね肉（皮なし）", "鶏むね肉 300g", "鶏胸肉", "サラダチキン（プレーン）", "ささみ 2本",
    "蒸し鶏（むね肉）", "鶏むね肉のグリル", "茹で鶏むね肉", "低温調理の鶏むね肉",
])
def test_plain_lean_chicken_becomes_a_and_b0(name):
    r = {"foods": [_food(name, 1)]}
    m.recompute_totals(r)
    f = r["foods"][0]
    assert f["b_count"] == 0 and f["category"] == "A"
    assert f["b_adjusted_from"] == 1
    assert r["total_b_count"] == 0


@pytest.mark.parametrize("name", [
    "皮つき鶏むね肉", "鶏むね肉の唐揚げ", "鶏むね肉のマヨ和え", "鶏むね肉の照り焼き", "チキン南蛮（むね肉）",
    "ささみフライ", "ささみチーズカツ", "親子丼", "鶏もも肉", "チキンカツ", "鶏むね肉の天ぷら",
])
def test_dishes_with_oil_or_sauce_are_left_alone(name):
    r = {"foods": [_food(name, 1.5)]}
    m.recompute_totals(r)
    assert r["foods"][0]["b_count"] == 1.5
    assert "b_adjusted_from" not in r["foods"][0]


def test_member_correction_is_respected():
    r = {"foods": [_food("鶏むね肉", 1, reason="訂正反映：本人の申告どおりB1")]}
    m.recompute_totals(r)
    assert r["foods"][0]["b_count"] == 1


def test_already_a_is_unchanged():
    r = {"foods": [_food("鶏むね肉", 0, cat="A")]}
    m.recompute_totals(r)
    assert r["foods"][0]["b_count"] == 0 and "b_adjusted_from" not in r["foods"][0]


def test_prompt_says_large_portions_stay_b0():
    assert "鶏むね肉（皮なし）・ささみ・サラダチキンは**300gでも500gでもB0**" in m.ANALYSIS_PROMPT


# ── 量ボタン（アプリ） ─────────────────────────────────────────
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")


def _run_scale(cases):
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    src = re.search(r"function bFromKcal\(kcal\) \{.*?\nfunction scaledBCount\(food, base, factor\) \{.*?\n\}",
                    html, re.S).group(0)
    harness = f"""{src}
const cases = {json.dumps(cases, ensure_ascii=False)};
console.log(JSON.stringify(cases.map(c => scaledBCount(c.food, c.base, c.factor))));
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


def _c(name, cat, b, kcal, factor):
    return {"food": {"name": name, "category": cat}, "base": {"b_count": b, "kcal_per_serving": kcal}, "factor": factor}


@needs_node
def test_quantity_buttons_keep_a_foods_at_zero():
    out = _run_scale([
        _c("鶏むね肉 100g", "A", 0, 105, 2),     # 以前は 210kcal → B1
        _c("鶏むね肉 100g", "A", 0, 105, 1.5),   # 以前は 158kcal → B0.5
        _c("ささみ", "A", 0, 98, 2),             # 以前は 196kcal → B0.5
        _c("鶏むね肉 150g", "A", 0, 158, 2),
        _c("ブロッコリー", "A", 0, 50, 2),
        _c("白米", "B", 1, 250, 2),              # B食材は今までどおり kcal から → B2
    ])
    assert out == [0, 0, 0, 0, 0, 2]
