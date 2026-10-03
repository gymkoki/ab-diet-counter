# 履歴タブ「あなたの1日平均Bカウント」の回帰テスト（会員の声 2026-10）。
#
# 経緯：履歴の上部は全期間の平均を整数に丸めた「1日平均 4回/日」だけで、ほとんど動かず、
#   会員は「これまでの自分の平均」なのか目標なのか分からなかった。
#   直近7日・直近30日・全期間を小数1桁で並べ、目標は別の文で示す。
#
# 実際の index.html から historyAverages を取り出し、Node.js で動かして検証する。

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


def _run(daily, today):
    m = re.search(r"const HIST_AVG_WINDOWS = \[.*?\nfunction historyAverages\(.*?\n\}", _html(), re.S)
    assert m, "index.html から historyAverages を取り出せません"
    harness = f"""
{m.group(0)}
const out = historyAverages({json.dumps(daily)}, {json.dumps(today)});
console.log(JSON.stringify(Object.fromEntries(out.map(a => [a.key, a]))));
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


TODAY = "2026-10-03"


def _day(date, b):
    return {"date": date, "b_count": b, "weight": None}


@needs_node
def test_windows_are_separate():
    daily = [_day("2026-10-02", 3), _day("2026-09-30", 4),       # 直近7日
             _day("2026-09-20", 6),                               # 直近30日
             _day("2026-07-01", 9)]                               # 全期間だけ
    r = _run(daily, TODAY)
    assert r["d7"] == {"key": "d7", "label": "直近7日", "days": 2, "avg": 3.5}
    assert r["d30"]["days"] == 3 and r["d30"]["avg"] == pytest.approx(4.3)
    assert r["all"]["days"] == 4 and r["all"]["avg"] == 5.5


@needs_node
def test_today_is_excluded_because_it_is_still_in_progress():
    r = _run([_day(TODAY, 1), _day("2026-10-02", 5)], TODAY)
    assert r["d7"]["avg"] == 5 and r["d7"]["days"] == 1


@needs_node
def test_days_without_records_are_not_counted_as_zero():
    r = _run([_day("2026-10-02", 4), _day("2026-10-01", None), _day("2026-09-29", 6)], TODAY)
    assert r["d7"]["avg"] == 5 and r["d7"]["days"] == 2


@needs_node
def test_seven_day_window_boundary():
    """直近7日＝昨日から数えて7日分（今日の7日前まで含み、8日前は含まない）。"""
    r = _run([_day("2026-09-26", 2), _day("2026-09-25", 10)], TODAY)
    assert r["d7"]["days"] == 1 and r["d7"]["avg"] == 2


@needs_node
def test_no_past_records_gives_empty_values():
    r = _run([_day(TODAY, 3)], TODAY)
    assert all(r[k]["avg"] is None and r[k]["days"] == 0 for k in ("d7", "d30", "all"))


@needs_node
def test_average_keeps_one_decimal():
    """整数に丸めると毎日同じ数字に見えてしまう（以前の「4回/日」問題）。"""
    r = _run([_day("2026-10-02", 3.5), _day("2026-10-01", 4)], TODAY)
    assert r["d7"]["avg"] == 3.8


def test_label_is_clearly_personal_and_target_is_separate():
    html = _html()
    assert "あなたの1日平均Bカウント" in html
    paint = re.search(r"function _paintHistory\(daily\) \{.*?\n\}", html, re.S).group(0)
    assert "回/日" not in paint, "全期間を整数に丸めた旧表示が残っている"
    assert "Math.round(sum / withB.length)" not in paint
    assert "historyAverages(daily, getTodayJST())" in paint
    assert "目標は" in paint and "1日 B${tgt.label}" in paint, "目標と自分の平均の違いが伝わらない"
