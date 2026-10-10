"""運動の記録を削除・修正できることの回帰テスト（会員の声 2026-10）。

指摘：「運動を一度間違えて入力してしまうと、削除が出来ない」。
原因：①一覧に削除ボタンが無かった（deleteExercise はあったが誰も呼んでいなかった）
      ②合計を「端末とサーバーの大きい方」で決めていたため、消してもサーバーに残った
        古い値で −B が復活した（時間を短く直したときも同じ）。
対策：削除ボタンを付け、この端末で削除・修正した日は端末の記録を正としてサーバーも上書きする。
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def _fn(html, name):
    m = re.search(r"function " + name + r"\(.*?\n\}\n", html, re.S)
    assert m, name
    return m.group(0)


def test_log_rows_have_a_delete_button():
    body = _fn(_html(), "renderExLog")
    assert 'onclick="deleteExercise(${l.ts})"' in body, "運動記録の一覧に削除ボタンが無い"


def test_delete_asks_for_confirmation():
    assert "confirm(" in _fn(_html(), "deleteExercise")


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_deleted_exercise_does_not_come_back_from_server():
    """サーバーに古い値（B1）が残っていても、削除した日は0になり、サーバーへ0を送る。"""
    html = _html()
    js = """
const store = {};
const localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
let confirmed = true; const confirm = () => confirmed;
const sent = [];
function submitDailyExercise(v, d) { sent.push([d, v]); }
function renderExLog() {} function updateDailyTotal() {}
let _editingTs = null; let _serverExByDate = {};
const document = { querySelector: () => ({}), getElementById: () => ({ style: {} }) };
""" + html[html.index("const EXERCISE_KEY"):html.index("// 散歩と室内散歩")] \
        + _fn(html, "_localExBCountForDate") + _fn(html, "getExBCountForDate") + _fn(html, "deleteExercise") + """
const D = '2026-10-09';
localStorage.setItem(EXERCISE_KEY, JSON.stringify([{ date: D, type: '筋トレ', bc: 1, ts: 1 }]));
_serverExByDate[D] = 1;                       // サーバーにも B1 が入っている
const before = getExBCountForDate(D);
confirmed = false; deleteExercise(1);         // キャンセルなら消さない
const afterCancel = getExBCountForDate(D);
confirmed = true; deleteExercise(1);
const after = getExBCountForDate(D);
_serverExByDate[D] = 1;                       // 同期で古いサーバー値が来ても…
const afterSync = getExBCountForDate(D);
console.log(JSON.stringify({ before, afterCancel, after, afterSync, sent }));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(js)
        path = tmp.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        import json
        d = json.loads(proc.stdout)
    finally:
        os.unlink(path)
    assert d["before"] == 1 and d["afterCancel"] == 1
    assert d["after"] == 0, "削除したのに −B が残っている"
    assert d["afterSync"] == 0, "サーバーの古い値で −B が復活している"
    assert d["sent"] == [["2026-10-09", 0]], "サーバーへ0を送っていない"


def test_sync_resends_touched_days():
    """通信に失敗していても、次の同期で端末の値を送り直す。"""
    body = _fn(_html(), "syncHistoryFromServer")
    assert "_getExTouched()[dd.date]" in body and "submitDailyExercise(mine, dd.date)" in body
