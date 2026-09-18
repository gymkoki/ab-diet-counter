# 「1日1食しか記録が無い日は危機管鳥だけを出す」の回帰テスト（オーナー指示 2026-09）。
#
# 経緯：1日1食しか記録が無い人は、たいてい写真がうまく上がっていないだけで、
#   実際にはもっと食べている。そのまま計算すると見かけの点数が高くなり
#   （キーラン君・成果フンバ）、記録できていないことに本人も運営も気づけない。
#   そこで記録が1食だけの日はスコアを危機管鳥の範囲（〜29点）に抑える。
#
# 【重要】判定するのは「記録できた食事の数」であって、写真の枚数ではない。
#   写真が1枚でも、文章などで2食以上記録できていれば通常どおり計算する。
#
# 実際の index.html から scoreFromTotals を取り出し、Node.js で動かして検証する
# （文字列の一致だけでなく、計算そのものが正しいことを確かめる）。

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "templates", "index.html")

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node が見つからないためスキップ")


def _html():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def _extract(pattern):
    m = re.search(pattern, _html(), re.S)
    assert m, f"index.html から取り出せません: {pattern}"
    return m.group(0)


def _run_score(cases):
    """実物の scoreFromTotals を Node で動かし、各ケースの結果を返す。"""
    src = _extract(r"const CHARA_KIKI_MAX_SCORE = .*?\nfunction scoreFromTotals\(.*?\n\}")
    harness = f"""
// 依存する設定は固定値で差し替える（体重70kg想定・目標B8回）
const VEG_TARGET_G = 350;
function getProteinTarget() {{ return {{ min: 80 }}; }}
function getGoalTarget() {{ return {{ max: 8 }}; }}
function getProfile() {{ return {{}}; }}
{src}
const cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map(c =>
  scoreFromTotals(c.foodTotal, c.protein, c.veg, c.exBc, c.mealCount))));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(harness)
        path = tmp.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
        assert proc.returncode == 0, f"実行に失敗:\n{proc.stderr}"
        return json.loads(proc.stdout)
    finally:
        os.unlink(path)


# 満点が出る条件（B目標内・タンパク質100%・野菜100%）をベースにする
PERFECT = {"foodTotal": 4, "protein": 80, "veg": 350, "exBc": 0}


@needs_node
def test_one_meal_day_is_capped_to_kikikancho():
    """満点の内容でも、記録が1食だけなら危機管鳥の範囲（〜29点）に抑える。"""
    [one] = _run_score([{**PERFECT, "mealCount": 1}])
    assert one["tooFewMeals"] is True
    assert one["score"] <= 29, "1食だけの日に成果フンバ／キーラン君が出てしまう"


@needs_node
def test_two_meals_are_scored_normally():
    """2食以上記録できていれば、写真が1枚でも通常どおりの採点になる。"""
    [two] = _run_score([{**PERFECT, "mealCount": 2}])
    assert two["tooFewMeals"] is False
    assert two["score"] == 100, "2食記録した日まで点数が下がっている"


@needs_node
def test_three_meals_are_scored_normally():
    [three] = _run_score([{**PERFECT, "mealCount": 3}])
    assert three["score"] == 100


@needs_node
def test_unknown_meal_count_is_not_capped():
    """食事数が分からない呼び出し（旧データの復元など）は従来どおり計算する。"""
    [unknown] = _run_score([{**PERFECT, "mealCount": None}])
    assert unknown["tooFewMeals"] is False
    assert unknown["score"] == 100


@needs_node
def test_cap_does_not_raise_a_bad_day():
    """もともと低い日を、この仕組みで持ち上げてしまわないこと（上限であって固定値ではない）。"""
    [bad] = _run_score([{"foodTotal": 0, "protein": 0, "veg": 0, "exBc": 0, "mealCount": 1}])
    assert bad["score"] == 0


@needs_node
def test_b_over_cap_still_works():
    """既存の「Bオーバーの日は危機管鳥」も壊れていないこと。"""
    [over] = _run_score([{"foodTotal": 20, "protein": 80, "veg": 350,
                          "exBc": 0, "mealCount": 3}])
    assert over["bOver"] is True and over["score"] <= 29


def test_kikikancho_covers_0_to_29():
    """〜29点が危機管鳥であること（上限値29の根拠。ここがずれると別キャラが出る）。"""
    patterns = _extract(r"const CHARA_PATTERNS = \[.*?\n\];")
    chars = re.findall(r"\{\s*c:'(\w+)'", patterns)
    assert len(chars) == 20, "5点刻み20帯から変わっている"
    for i in range(6):          # 0-4, 5-9, ... 25-29 点
        assert chars[i] == "kiki", f"{i*5}〜{i*5+4}点が危機管鳥ではない"
    assert chars[6] != "kiki", "30点以上まで危機管鳥になっている（抑えすぎ）"


def _score_call_args(html):
    """scoreFromTotals(...) の呼び出し引数を、入れ子の括弧も含めて取り出す。"""
    out = []
    for m in re.finditer(r"scoreFromTotals\(", html):
        i = m.end()
        depth, start = 1, i
        while i < len(html) and depth:
            if html[i] == "(":
                depth += 1
            elif html[i] == ")":
                depth -= 1
            i += 1
        out.append(html[start:i - 1])
    return out


def _top_level_arg_count(args):
    """入れ子の括弧の中のカンマは数えずに、引数の数を返す。"""
    depth, n = 0, 1
    for ch in args:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            n += 1
    return n


def test_every_score_call_site_passes_the_meal_count():
    """スコアを保存する経路すべてで食事数を渡すこと。
    渡し忘れた経路があると、その日だけ抜け道になって高得点が残る。"""
    html = _html()
    calls = [a for a in _score_call_args(html) if "foodTotal" in a and "mealCount" not in a]
    assert len(calls) >= 3, "スコア計算の呼び出しが見つからない"
    for args in calls:
        assert _top_level_arg_count(args) >= 5, \
            f"食事数を渡していない呼び出しがある: scoreFromTotals({args})"


def test_advice_explains_why():
    """1食だけの日は、点数が伸びない理由と対処（記録の追加）を伝えること。"""
    html = _html()
    advice = re.search(r"function charaAdvice\(s\) \{.*?\n\}", html, re.S).group(0)
    assert "s.tooFewMeals" in advice, "1食だけの日の案内が無い"
    assert "1食" in advice and "2食以上" in advice
