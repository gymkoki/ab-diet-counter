# 果物ジュースの特別ルールの回帰テスト（オーナー指示 2026-11-08）。
#
# 指摘：「（野菜ジュースは）野菜をスムージーにしたので、A食材で良いが、果物ジュースの場合は
#   寿司などの特別ルールを適用して、1本ではなく、1日当たりの総カロリーに合わせてBカウントを追加するようにして」
#
# 仕組み：
#   ・果物ジュースの各項目に juice_kcal（飲んだ分のkcal）を持たせる。AIが入れ忘れても名前から補う。
#   ・その日の合計kcalでSTEP2を当て、ジュースの項目に既に付いたBとの差を足す（寿司と同じ）。
#   ・これまでのお菓子・スイーツ・ジュースのルール（最大+1）と比べて大きいほうを採る
#     （以前より少なく数える日を出さない）。
#   ・野菜だけのジュース・スムージーは対象外（A食材のまま）。
#   ・アプリ（index.html）とサーバー（_summarize_day_meals）で同じ計算にする。
import copy
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
from conftest import seed_weight  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "templates", "index.html")


def _drink(name, kcal, b=0, juice=None, sweet=None):
    f = {"name": name, "category": "B" if b else "A", "kcal_per_serving": kcal, "b_count": b,
         "protein_g": 0, "veg_g": 0, "amount": "", "reason": "",
         "sweet_kcal": kcal if sweet is None else sweet}
    if juice is not None:
        f["juice_kcal"] = juice
    return f


ORANGE = _drink("果汁100% オレンジジュース（200ml）", 90)


def _filled(foods):
    foods = copy.deepcopy(foods)
    m._fill_juice_kcal(foods)
    return foods


# ── どれが果物ジュースか ───────────────────────────────────
@pytest.mark.parametrize("name", [
    "果汁100% オレンジジュース（200ml）", "りんごジュース", "アップルジュース 1本",
    "グレープジュース", "ミックスジュース", "マンゴースムージー", "いちごバナナスムージー",
    "トロピカーナ ピーチネクター", "野菜と果物のミックスジュース（りんご果汁入り）",
])
def test_fruit_juice_gets_juice_kcal(name):
    foods = _filled([_drink(name, 90)])
    assert foods[0]["juice_kcal"] == 90


@pytest.mark.parametrize("name", [
    "カゴメ つぶより野菜（195ml）", "トマトジュース", "野菜ミックスジュース", "青汁",
    "グリーンスムージー（小松菜・ほうれん草）", "コーラ 350ml", "スポーツドリンク",
    "オレンジゼリー", "グレープフルーツサワー", "みかん（1個）", "りんご",
])
def test_non_fruit_juice_is_left_alone(name):
    foods = _filled([_drink(name, 90)])
    assert "juice_kcal" not in foods[0]


def test_ai_value_is_kept():
    foods = _filled([_drink("フルーツスムージー", 180, b=0.5, juice=180)])
    assert foods[0]["juice_kcal"] == 180
    foods = _filled([_drink("オレンジジュース", 90, juice=0)])   # AIが対象外と判断したら尊重する
    assert foods[0]["juice_kcal"] == 0


# ── 1日の累積 ────────────────────────────────────────────────
@pytest.mark.parametrize("bottles,bonus", [
    (1, 0.0),     # 90kcal  → B0
    (2, 0.5),     # 180kcal → B0.5
    (3, 1.0),     # 270kcal → B1
    (5, 2.0),     # 450kcal → B2（以前のお菓子ルールでは +1 止まり）
    (7, 2.5),     # 630kcal → B2.5
])
def test_juice_is_counted_by_daily_total(bottles, bonus):
    foods = _filled([ORANGE] * bottles)
    assert m._day_juice_bonus(foods)[1] == bonus


def test_b_already_given_is_not_counted_twice():
    """500ml（225kcal）で既にB1が付いていれば、合計（B1相当）との差は0。"""
    foods = _filled([_drink("オレンジジュース 500ml", 225, b=1)])
    assert m._day_juice_bonus(foods)[1] == 0


def test_never_lower_than_the_old_sweet_rule():
    """少量のジュース＋お菓子の日は、これまでのお菓子ルール（合計140kcal → +0.5）のまま。"""
    foods = _filled([ORANGE, _drink("クッキー1枚", 50)])
    sj = m._day_sweet_and_juice(foods)
    assert sj["sweet_bonus"] + sj["juice_bonus"] == 0.5


def test_many_juices_plus_sweets():
    """ジュース6本（540kcal → +2）＋チョコ150kcal（お菓子ルール +0.5）＝ +2.5。"""
    foods = _filled([ORANGE] * 6 + [_drink("チョコ", 150)])
    sj = m._day_sweet_and_juice(foods)
    assert sj["juice_bonus"] == 2.0 and sj["sweet_bonus"] == 0.5


def test_vegetable_juice_stays_a():
    """オーナーの写真の例：野菜ジュースは何本飲んでも果物ジュースのルールでは増えない。"""
    veg = _drink("カゴメ つぶより野菜（195ml）", 70, sweet=0)
    foods = _filled([veg] * 6)
    sj = m._day_sweet_and_juice(foods)
    assert sj["juice_bonus"] == 0 and sj["sweet_bonus"] == 0


def _payload(*foods_per_item):
    items = []
    for foods in foods_per_item:
        foods = _filled(foods)
        items.append({"result": {"foods": foods, "total_b_count": sum(f["b_count"] for f in foods)}})
    return json.dumps({"food": {"items": items}})


def test_day_summary_adds_juice_across_records():
    """1本ずつ別々に記録しても、1日の合計で数える。"""
    p = _payload([_drink("白米", 250, b=1, sweet=0)], [ORANGE], [ORANGE], [ORANGE])   # 270kcal → +1
    s = m._summarize_day_meals(p)
    assert s["juice_bonus"] == 1.0 and s["b_count"] == 2.0


def test_day_without_juice_is_unchanged():
    p = _payload([_drink("白米", 250, b=1, sweet=0)])
    s = m._summarize_day_meals(p)
    assert s["b_count"] == 1.0 and s["juice_bonus"] == 0


# ── 解析の入口を通ると juice_kcal が付いて返る ─────────────────
@pytest.fixture
def client(monkeypatch):
    m.app.config["TESTING"] = True
    monkeypatch.setattr(m, "get_client", lambda: object())
    seed_weight("juice-user")
    with m.app.test_client() as c:
        yield c


def test_analyze_text_fills_juice_kcal(client, monkeypatch):
    reply = {"foods": [copy.deepcopy(ORANGE), _drink("トマトジュース", 40, sweet=0)],
             "total_b_count": 0, "total_protein_g": 0, "total_veg_g": 0, "advice": ""}
    monkeypatch.setattr(m, "_create_with_server_tools", lambda *a, **k: None)
    monkeypatch.setattr(m, "parse_ai_result", lambda resp: copy.deepcopy(reply))
    r = client.post("/analyze-text", data={"text": "オレンジジュースとトマトジュース", "user_id": "juice-user"})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert [f.get("juice_kcal") for f in d["foods"]] == [90, None]
    assert d["total_juice_kcal"] == 90


# ── プロンプト ───────────────────────────────────────────────
def test_prompt_asks_for_juice_kcal():
    p = m.ANALYSIS_PROMPT
    assert p.count('"juice_kcal"') == 4, "各食材のスキーマに juice_kcal が無い"
    assert "果物ジュース(juice_kcal)の推定ルール" in p
    assert "野菜だけのジュース・スムージー" in p


# ── アプリ（JS）もサーバーと同じ計算 ─────────────────────────────
def _html():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def _extract(html, start_marker, fn_name):
    start = html.index(start_marker)
    m_ = re.search(r"\nfunction " + re.escape(fn_name) + r"\(items\) \{.*?\n\}", html[start:], re.S)
    return html[start:start + m_.end()]


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_frontend_matches_server():
    html = _html()
    sweet = _extract(html, "const SWEET_DAY_HALF_KCAL", "daySweetBonusB")
    bfk = re.search(r"function bFromKcal\(kcal\) \{.*?\n\}", html, re.S).group(0)
    juice = _extract(html, "function dayJuiceBonus(items)", "daySweetAndJuice")
    days = [
        [ORANGE] * n for n in (0, 1, 2, 3, 5, 6)
    ] + [
        [ORANGE, _drink("クッキー1枚", 50)],
        [ORANGE] * 6 + [_drink("チョコ", 150)],
        [_drink("オレンジジュース 500ml", 225, b=1)],
        [ORANGE, ORANGE, _drink("グミ", 80)],
        [_drink("カゴメ つぶより野菜", 70, sweet=0)] * 6,
    ]
    days = [_filled(d) for d in days]
    harness = f"""{sweet}
{bfk}
{juice}
const days = {json.dumps(days, ensure_ascii=False)};
console.log(JSON.stringify(days.map(foods => daySweetAndJuice([{{ result: {{ foods }} }}]))));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(harness)
        path = tmp.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        js = json.loads(proc.stdout)
    finally:
        os.unlink(path)
    for d, j in zip(days, js):
        s = m._day_sweet_and_juice(d)
        assert (j["sweetBonus"], j["juiceBonus"]) == (s["sweet_bonus"], s["juice_bonus"]), d
        assert round(j["sweetKcal"], 1) == round(s["sweet_kcal"], 1)


def test_daily_total_includes_juice():
    html = _html()
    assert "foodTotal += sj.juiceBonus;" in html
    assert "sj.sweetBonus + sj.juiceBonus" in html, "daySpecialBonusB に果物ジュースの分が入っていない"


def test_banner_is_shown():
    bar = re.search(r'<div id="juice-bonus-bar".*?\n  </div>', _html(), re.S).group(0)
    assert "🧃 特別ルール適用！" in bar and "果物ジュース 合計" in bar


def test_qty_buttons_scale_juice_kcal():
    html = _html()
    assert "juice_kcal: food.juice_kcal," in html
    assert "food.juice_kcal = Math.round((Number(base.juice_kcal) || 0) * next);" in html
