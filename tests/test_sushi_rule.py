# 寿司のシャリの特別ルールの回帰テスト（オーナー指示 2026-10-13）。
#
# 指摘：「寿司はお菓子同様、特別ルール適用です。1貫あたりの白米の量はB0ですが、
#   寿司全部の白米の量に換算するとB1からB3ぐらいいくと思います。
#   寿司の場合はトータル量を加味してBカウントをしてください。」
#
# 仕組み：
#   ・各寿司の項目に sushi_rice_g（シャリの重量）を持たせる。AIが入れ忘れても「◯貫」から補う。
#   ・その日のシャリの合計kcal（1g≒1.68kcal）でSTEP2を当て、寿司の項目に既に付いたBとの差を足す。
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


def _sushi(name, b=0, rice=None, kcal=60):
    f = {"name": name, "category": "B", "kcal_per_serving": kcal, "b_count": b,
         "protein_g": 5, "veg_g": 0, "amount": "", "reason": ""}
    if rice is not None:
        f["sushi_rice_g"] = rice
    return f


# オーナーの写真の例：1貫ずつ4項目、どれもB0
OWNER_CASE = [
    _sushi("白身魚の握り（1貫）"),
    _sushi("炙りのどぐろ（または青魚）の握り（1貫）"),
    _sushi("まぐろ（ねぎとろ風）軍艦（1貫）"),
    _sushi("ガリ（生姜）", kcal=5),
]


# ── STEP2 のしきい値 ─────────────────────────────────────────
@pytest.mark.parametrize("kcal,b", [
    (0, 0), (119, 0), (120, 0.5), (200, 0.5), (201, 1), (400, 1),
    (401, 2), (600, 2), (601, 2.5), (800, 2.5), (801, 3),
])
def test_b_from_kcal_matches_step2(kcal, b):
    assert m._b_from_kcal(kcal) == b


# ── シャリの重量を補う ────────────────────────────────────────
@pytest.mark.parametrize("name,expected", [
    ("まぐろの握り ×3貫", 54),
    ("白身魚の握り（1貫）", 18),
    ("いくら軍艦 ２貫", 36),               # 全角数字
    ("サーモン寿司 5貫", 90),
])
def test_rice_is_filled_from_piece_count(name, expected):
    foods = [_sushi(name)]
    m._fill_sushi_rice(foods)
    assert foods[0]["sushi_rice_g"] == expected


@pytest.mark.parametrize("name", ["おにぎり（鮭）", "春巻き2本", "白米", "ガリ（生姜）"])
def test_non_sushi_is_left_alone(name):
    foods = [_sushi(name)]
    m._fill_sushi_rice(foods)
    assert "sushi_rice_g" not in foods[0]


def test_ai_value_is_kept():
    foods = [_sushi("ちらし寿司", b=1.5, rice=200)]
    m._fill_sushi_rice(foods)
    assert foods[0]["sushi_rice_g"] == 200


# ── 1日の累積 ────────────────────────────────────────────────
def _filled(foods):
    foods = copy.deepcopy(foods)
    m._fill_sushi_rice(foods)
    return foods


def test_owner_case_adds_up_across_the_meal():
    """写真の3貫（シャリ54g≒91kcal）だけではまだ120kcal未満でB0だが、
    同じ日にもう3貫食べれば（108g≒181kcal）B0.5が足される。"""
    rice, bonus = m._day_sushi_bonus(_filled(OWNER_CASE))
    assert rice == 54 and bonus == 0
    rice, bonus = m._day_sushi_bonus(_filled(OWNER_CASE + OWNER_CASE))   # 6貫＝108g≒181kcal
    assert rice == 108 and bonus == 0.5


@pytest.mark.parametrize("pieces,bonus", [
    (8, 1.0),     # 144g ≒ 242kcal → B1
    (12, 1.0),    # 216g ≒ 363kcal → B1
    (15, 2.0),    # 270g ≒ 454kcal → B2
    (20, 2.5),    # 360g ≒ 605kcal → B2.5
])
def test_whole_sushi_meal_reaches_b1_to_b3(pieces, bonus):
    foods = _filled([_sushi("握り（1貫）") for _ in range(pieces)])
    assert m._day_sushi_bonus(foods)[1] == bonus


def test_b_already_given_is_not_counted_twice():
    """「握り ×8貫」で既にB1が付いていれば、シャリ合計（B1相当）との差は0。"""
    foods = _filled([_sushi("握り ×8貫", b=1, kcal=420)])
    assert m._day_sushi_bonus(foods)[1] == 0


def test_partial_credit_when_some_b_already_given():
    foods = _filled([_sushi("サーモンの握り ×4貫", b=0.5, kcal=200)]
                    + [_sushi("白身の握り（1貫）") for _ in range(8)])   # 計12貫＝216g≒363kcal → B1
    assert m._day_sushi_bonus(foods)[1] == 0.5


def _payload(*foods_per_item):
    items = []
    for foods in foods_per_item:
        foods = _filled(foods)
        items.append({"result": {"foods": foods, "total_b_count": sum(f["b_count"] for f in foods)}})
    return json.dumps({"food": {"items": items}})


def test_day_summary_adds_sushi_bonus_across_photos():
    """寿司は何枚かに分けて撮ることが多い。写真をまたいでシャリを合計する。"""
    four = [_sushi("握り（1貫）") for _ in range(4)]
    p = _payload([_sushi("白米", b=1, kcal=250)], four, four)   # 8貫＝144g → +1
    s = m._summarize_day_meals(p)
    assert s["sushi_bonus"] == 1.0 and s["b_count"] == 2.0


def test_day_without_sushi_is_unchanged():
    p = _payload([_sushi("白米", b=1, kcal=250)])
    assert m._summarize_day_meals(p)["b_count"] == 1.0


# ── 解析の入口を通ると sushi_rice_g が付いて返る ─────────────────
@pytest.fixture
def client(monkeypatch):
    m.app.config["TESTING"] = True
    monkeypatch.setattr(m, "get_client", lambda: object())
    seed_weight("sushi-user")
    with m.app.test_client() as c:
        yield c


def test_analyze_text_fills_rice(client, monkeypatch):
    reply = {"foods": copy.deepcopy(OWNER_CASE), "total_b_count": 0, "total_protein_g": 15,
             "total_veg_g": 0, "advice": ""}
    monkeypatch.setattr(m, "_create_with_server_tools", lambda *a, **k: None)
    monkeypatch.setattr(m, "parse_ai_result", lambda resp: copy.deepcopy(reply))
    r = client.post("/analyze-text", data={"text": "寿司4貫", "user_id": "sushi-user"})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert [f.get("sushi_rice_g") for f in d["foods"]] == [18, 18, 18, None]
    assert d["total_sushi_rice_g"] == 54


# ── プロンプト ───────────────────────────────────────────────
def test_prompt_asks_for_sushi_rice():
    p = m.ANALYSIS_PROMPT
    assert p.count('"sushi_rice_g"') == 4, "各食材のスキーマに sushi_rice_g が無い"
    assert "寿司のシャリ(sushi_rice_g)の推定ルール" in p
    assert "1貫≒18g" in p


# ── アプリ（JS）もサーバーと同じ計算 ─────────────────────────────
def _html():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_frontend_matches_server():
    src = re.search(r"const SUSHI_RICE_KCAL_PER_G = .*?\nfunction daySushiBonus\(items\) \{.*?\n\}",
                    _html(), re.S).group(0)
    days = [
        [_sushi("握り（1貫）") for _ in range(n)] for n in (0, 3, 6, 8, 15, 20)
    ] + [
        [_sushi("握り ×8貫", b=1)],
        [_sushi("サーモンの握り ×4貫", b=0.5)] + [_sushi("白身の握り（1貫）") for _ in range(8)],
    ]
    days = [_filled(d) for d in days]
    harness = f"""{src}
const days = {json.dumps(days, ensure_ascii=False)};
console.log(JSON.stringify(days.map(foods => daySushiBonus([{{ result: {{ foods }} }}]).bonus)));
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
    assert js == [m._day_sushi_bonus(d)[1] for d in days]


def test_daily_total_includes_sushi_everywhere():
    """Bの合計を出すすべての箇所で寿司の分も足す（1か所でも漏れると画面ごとに合計が変わる）。"""
    html = _html()
    assert "daySweetBonusB(allItems);\n  foodTotal += sweetBonus;" in html
    assert "foodTotal += sushi.bonus;" in html
    assert html.count("daySpecialBonusB(") >= 5
    assert "foodTotal += daySweetBonusB(" not in html, "寿司の分を足していない合計がある"


def test_banner_is_shown():
    bar = re.search(r'<div id="sushi-bonus-bar".*?\n  </div>', _html(), re.S).group(0)
    assert "🍣 特別ルール適用！" in bar and "シャリ" in bar
