# 「計算し直す」フォームの上段（食べた食材＋B何回分をそのまま追加）の回帰テスト。
#
# 経緯（オーナー依頼）：例文を「白米を一杯食べているので、B1回追加」にし、
#   会員が「食材」と「B何回分」を選んでBカウントを追加できるようにした。
#   追加はAIを呼ばずに端末内で完結させる（待たせる経路に新しいAI往復を足さない＝CLAUDE.md）。
# Codex のレビュー（PR #255）で見つかった点も固定する：
#   ・文章訂正の進捗・無効化が「追加」ボタンに付き、「再計算する」が押せたままだった（二重送信）
#   ・kcal を範囲の文字列で保存していて、サーバーの1日集計から黙って抜けていた
#   ・「訂正反映」の印が無く、量ボタンやサーバーの安全網が会員の指定Bを書き換えていた

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")


def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def _fn(name):
    mt = re.search(r"function " + name + r"\(.*?\n\}", _html(), re.S)
    assert mt, f"{name} が見つかりません"
    return mt.group(0)


def _run(body):
    html = _html()
    bfrom = re.search(r"function bFromKcal\(kcal\) \{.*?\nfunction scaledBCount\(food, base, factor\) \{.*?\n\}",
                      html, re.S).group(0)
    harness = f"""
{bfrom}
{_fn("recalcResultTotals")}
{_fn("fmtCount")}
{_fn("addManualFoodB")}
const _els = {{}};
const document = {{ getElementById: id => _els[id] || null }};
let rendered = 0, saved = 0, totals = 0;
function renderResults() {{ rendered++; }}
function updateDailyTotal() {{ totals++; }}
function saveState() {{ saved++; }}
const state = {{ food: {{ items: [] }} }};
{body}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(harness)
        path = tmp.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True, encoding="utf-8")
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)
    finally:
        os.unlink(path)


_ADD_WHITE_RICE = """
state.food.items.push({ id: 1, result: {
  foods: [{ name: '焼き魚', category: 'A', kcal_per_serving: 200, b_count: 0, protein_g: 20, veg_g: 0 },
          { name: '白米', category: 'B', kcal_per_serving: 250, b_count: 1, protein_g: 4, veg_g: 0 }],
  total_b_count: 1, total_protein_g: 24, total_veg_g: 0 } });
_els['recalc-food-food-1'] = { value: '白米一杯', focus(){} };
_els['recalc-bcnt-food-1'] = { value: '%s' };
addManualFoodB('food', 1);
const r = state.food.items[0].result;
console.log(JSON.stringify({ r, rendered, saved, totals }));
"""


@needs_node
def test_add_white_rice_b1_updates_breakdown_totals_and_saves():
    """「白米一杯／B1回」で内訳に1行増え、合計Bが内訳と一致し、保存・再描画されること。"""
    out = _run(_ADD_WHITE_RICE % "1")
    r = out["r"]
    added = r["foods"][-1]
    assert len(r["foods"]) == 3
    assert added["name"] == "白米一杯" and added["category"] == "B" and added["b_count"] == 1
    assert r["total_b_count"] == 2 == sum(f["b_count"] for f in r["foods"])
    assert r["total_protein_g"] == 24, "追加でタンパク質など他の合計が変わってはいけない"
    assert out["saved"] == 1 and out["rendered"] == 1 and out["totals"] == 1


@needs_node
@pytest.mark.parametrize("b", ["0.5", "1", "2", "2.5", "3"])
def test_added_kcal_is_numeric_and_maps_back_to_the_chosen_b(b):
    """kcal は数値（サーバーの1日集計 `_summarize_day_meals` は数値しか足さない）で、
    STEP2（bFromKcal）に当てると会員が選んだBに戻ること。"""
    body = (_ADD_WHITE_RICE % b).replace(
        "console.log(JSON.stringify({ r, rendered, saved, totals }));",
        "const f = r.foods.at(-1); console.log(JSON.stringify({ kcal: f.kcal_per_serving, back: bFromKcal(f.kcal_per_serving) }));")
    out = _run(body)
    assert isinstance(out["kcal"], (int, float)) and out["kcal"] > 0
    assert out["back"] == float(b)


@needs_node
def test_manual_row_is_marked_as_member_correction():
    """会員の申告として「訂正反映」の印が付くこと（量ボタン・サーバーの安全網が指定Bを書き換えないように）。"""
    out = _run(_ADD_WHITE_RICE % "1")
    assert out["r"]["foods"][-1]["reason"].startswith("訂正反映")


@needs_node
def test_banana_added_by_member_scales_up_not_down():
    """「バナナ1本／B1回」を追加して1.5倍を押したら、Bは増える（早見表でB0.5に下げない）。"""
    body = (_ADD_WHITE_RICE % "1").replace("'白米一杯'", "'バナナ1本'").replace(
        "console.log(JSON.stringify({ r, rendered, saved, totals }));",
        "const f = r.foods.at(-1); console.log(JSON.stringify({ b: scaledBCount(f, f, 1.5) }));")
    assert _run(body)["b"] >= 1.5


@needs_node
def test_empty_food_name_does_nothing():
    """食材名が空なら何も追加しない（合計も保存も変えない）。"""
    body = (_ADD_WHITE_RICE % "1").replace("value: '白米一杯'", "value: '  '")
    out = _run(body)
    assert len(out["r"]["foods"]) == 2 and out["r"]["total_b_count"] == 1 and out["saved"] == 0


@needs_node
def test_add_is_blocked_while_text_correction_is_running():
    """文章訂正の再計算中（item.loading）は追加しない。応答で item.result が丸ごと置き換わり、
    追加した行が黙って消えるため（Codex 指摘 PR #255）。"""
    body = (_ADD_WHITE_RICE % "1").replace("state.food.items.push({ id: 1, result:",
                                           "state.food.items.push({ id: 1, loading: true, result:")
    out = _run(body)
    assert len(out["r"]["foods"]) == 2 and out["r"]["total_b_count"] == 1 and out["saved"] == 0


def test_reanalyze_disables_the_add_button_until_done():
    """再計算の送信中は追加ボタンも押せない。失敗時は押せる状態に戻す（成功時は再描画で戻る）。"""
    body = _fn("reanalyzeItem")
    assert ".btn-addb`)" in body
    assert body.count("addBtn.disabled = true") == 1
    assert body.count("addBtn.disabled = false") == 2, "エラー応答・通信失敗の両方で戻すこと"


def test_server_safety_nets_skip_member_corrections():
    """サーバーの安全網（バナナ・野菜）が「訂正反映」の行に触らないこと（印の前提）。"""
    import sys
    sys.path.insert(0, ROOT)
    import app as m
    res = {"foods": [{"name": "バナナ1本", "category": "B", "kcal_per_serving": 300, "amount": "",
                      "b_count": 1, "reason": "訂正反映：ユーザーの申告により追加（B1）"}]}
    m.recompute_totals(res)
    assert res["foods"][0]["b_count"] == 1
    assert res["total_b_count"] == 1


def test_manual_add_does_not_call_the_ai():
    """Bカウントの追加はAIを呼ばない（fetch しない）。"""
    body = _fn("addManualFoodB")
    assert "fetch" not in body and "reanalyze" not in body


def test_text_correction_button_is_distinct_from_add_button():
    """文章訂正の進捗・無効化は「再計算する」ボタンにだけ付くこと（二重送信を防ぐ）。
    追加ボタンも .btn-recalc を持つため、汎用の .btn-recalc で選ぶと先に追加ボタンが取れてしまう。"""
    html = _html()
    assert re.search(r'class="btn-recalc btn-recalc-text" onclick="reanalyzeItem\(', html)
    assert not re.search(r'class="[^"]*(btn-addb[^"]*btn-recalc-text|btn-recalc-text[^"]*btn-addb)[^"]*"', html)
    assert ".btn-recalc-text`)" in _fn("reanalyzeItem")


def test_form_wording():
    """オーナー指定の文言（ラベル・例文）。"""
    html = _html()
    assert "<label>気になる点・訂正を入力</label>" in html
    assert 'placeholder="例：白米を一杯食べているので、B1回追加"' in html
    assert "（より高精度なAIで計算し直します）" not in html
