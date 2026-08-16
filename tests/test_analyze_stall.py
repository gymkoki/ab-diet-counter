# 「解析が99%で止まる」障害の回帰テスト（オーナー報告 2026-08）。
#
# 症状：栄養解析の進捗が99.0%のまま二度と進まず、エラーも出ない。複数の会員で発生。
# 原因：
#   ① 端末側の fetch に時間制限が無く、スマホの回線が黙って切れると
#      「成功も失敗もしない」まま永久に待ち続けていた。
#   ② analyzeItem の途中で想定外のエラーが起きると、進捗タイマーが止まらず
#      99%のまま固まっていた。
#   ③ サーバー側で、Web検索の再開(pause_turn)が持ち時間の予算を突き抜けると
#      gunicorn にワーカーごと落とされ、解析中の他の会員まで巻き添えになっていた。
#
# ここでは「永久に待つ経路が残っていないこと」を機械的に固定する。

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


# ── ① 端末側：通信に必ず時間制限がある ──────────────────────────
def test_fetch_has_timeout():
    """解析の通信が AbortController で必ず打ち切られること。"""
    html = _read("templates/index.html")
    assert "ANALYZE_FETCH_TIMEOUT_MS" in html
    assert "new AbortController()" in html
    assert "signal: ctrl.signal" in html, "fetchにsignalを渡していないと時間制限が効かない"
    assert "clearTimeout(timer)" in html, "タイマーを片付けないと無駄に残る"


def test_fetch_timeout_is_longer_than_server_budget():
    """端末の時間制限は、サーバーが自分で答えを返す時間より長いこと。
    短いと、正常に解析中のリクエストを端末側が先に切ってしまう。"""
    html = _read("templates/index.html")
    fetch_ms = int(re.search(r"ANALYZE_FETCH_TIMEOUT_MS\s*=\s*(\d+)", html).group(1))
    # 写真の送信・画像処理・応答の送信ぶんの余裕（30秒以上）を必ず確保する。
    # ここが薄いと、正常に解析中のリクエストを端末が先に切ってしまう。
    margin_ms = fetch_ms - m.RETRY_TIME_BUDGET_SEC * 1000
    assert margin_ms >= 30000, f"端末の制限とサーバーの持ち時間の余裕が不足: {margin_ms / 1000:.0f}秒"


def test_progress_is_shown_as_percent():
    """解析中の表示は『％』であること（オーナー指示 2026-08：秒数表示は分かりにくい）。
    一度は経過秒数に変えたが、分かりにくいとの指摘を受けて％へ戻した。
    ％が99%に張り付いて見えても、下の各テストが担保する締め切りと見張りによって
    必ず有限時間で結論（結果かエラー）に到達する。"""
    html = _read("templates/index.html")
    assert "${fmtPct(item.loadingPct)}%" in html, "サムネイルの表示が％になっていない"
    assert "elapsedLabel" not in html, "秒数表示の関数が残っている"


def test_elapsed_time_is_still_recorded_for_diagnosis():
    """画面には出さないが、かかった秒数はコンソールに残すこと（遅いときの調査用）。"""
    html = _read("templates/index.html")
    assert "function _elapsedSec" in html
    assert "解析にかかった時間" in html
    assert "loadingStartedAt" in html


def test_waiting_time_is_kept_short():
    """会員を待たせる時間の上限が、実用的な長さに収まっていること。"""
    html = _read("templates/index.html")
    fetch_ms = int(re.search(r"ANALYZE_FETCH_TIMEOUT_MS\s*=\s*(\d+)", html).group(1))
    total_ms = int(re.search(r"ANALYZE_TOTAL_DEADLINE_MS\s*=\s*(\d+)", html).group(1))
    assert fetch_ms <= 180000, "1回の通信で3分以上待たせない"
    assert total_ms <= 300000, "全体で5分以上待たせない"
    assert m.RETRY_TIME_BUDGET_SEC <= 120.0, "サーバーが粘りすぎている（会員が待たされる）"


def test_total_deadline_exists_and_is_bounded():
    """再送も含めた全体の締め切りがあり、際限なく再送し続けないこと。"""
    html = _read("templates/index.html")
    total_ms = int(re.search(r"ANALYZE_TOTAL_DEADLINE_MS\s*=\s*(\d+)", html).group(1))
    fetch_ms = int(re.search(r"ANALYZE_FETCH_TIMEOUT_MS\s*=\s*(\d+)", html).group(1))
    assert fetch_ms <= total_ms <= 15 * 60 * 1000, "全体の締め切りが無い／長すぎる"
    # 呼び出し側が渡さなくても既定の締め切りが入ること
    assert "deadlineAt = deadlineAt || (Date.now() + ANALYZE_TOTAL_DEADLINE_MS)" in html


def test_timeout_is_reported_as_communication_error():
    """時間切れ(AbortError)を『画面の不具合』ではなく通信の失敗として扱うこと。"""
    html = _read("templates/index.html")
    assert "function _isTimeoutError" in html
    assert "AbortError" in html
    assert "_isTimeoutError(e) || (e instanceof TypeError)" in html


# ── ② 端末側：進捗タイマーが必ず止まる ──────────────────────────
def test_analyze_item_stops_progress_in_finally():
    """analyzeItem が何で失敗しても、finally で進捗を止めること。"""
    html = _read("templates/index.html")
    body = re.search(r"async function analyzeItem\(.*?\n\}", html, re.S).group(0)
    assert "finally {" in body, "analyzeItem に finally が無い（99%で固まる原因）"
    fin = body[body.index("finally {"):]
    assert "_stopProgress" in fin, "finally で進捗を止めていない"


def test_progress_has_watchdog():
    """止め忘れがあっても、見張りが強制的に進捗を終わらせること。"""
    html = _read("templates/index.html")
    assert "PROGRESS_WATCHDOG_MS" in html
    watchdog = int(re.search(r"PROGRESS_WATCHDOG_MS\s*=\s*(\d+)", html).group(1))
    total = int(re.search(r"ANALYZE_TOTAL_DEADLINE_MS\s*=\s*(\d+)", html).group(1))
    assert watchdog > total, "見張りが全体の締め切りより短いと、正常な解析を切ってしまう"
    body = re.search(r"function _startProgress\(.*?\n\}", html, re.S).group(0)
    assert "startedAt" in body and "_stopProgress" in body


def test_watchdog_constant_is_a_literal():
    """見張りの値は数値で書くこと。
    後ろで定義される const を参照すると読み込み時にエラーになり、
    1ファイルに全JSが入っているアプリ全体が起動しなくなる。"""
    html = _read("templates/index.html")
    assert re.search(r"PROGRESS_WATCHDOG_MS\s*=\s*\d+\s*;", html), \
        "PROGRESS_WATCHDOG_MS を計算式で書かないこと"


# ── ③ サーバー側：1リクエストが持ち時間の予算を超えない ───────────
def test_server_tools_respect_deadline():
    """Web検索の再開(pause_turn)が締め切りを守ること。"""
    src = _read("app.py")
    assert "def _create_with_server_tools(client, deadline=None" in src
    assert "create_and_parse" in src and "deadline=deadline" in src


class _Blk:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def test_pause_turn_stops_when_deadline_is_near():
    """締め切りが迫っていたら検索を再開せず、そこで打ち切ること。"""
    fake = _FakeClient([_Resp([_Blk("server_tool_use")], stop_reason="pause_turn") for _ in range(5)])
    m._create_with_server_tools(fake, deadline=time.monotonic() + 5.0,
                                messages=[{"role": "user", "content": "x"}])
    assert len(fake.messages.calls) == 1, "残り時間が無いのに再開している"


def test_each_call_gets_remaining_time_as_timeout():
    """1回のAI呼び出しに、残り時間ぶんの制限が渡されること。"""
    fake = _FakeClient([_Resp([_Blk("text", "{}")])])
    m._create_with_server_tools(fake, deadline=time.monotonic() + 30.0,
                                messages=[{"role": "user", "content": "x"}])
    t = fake.messages.calls[0]["timeout"]
    assert 25.0 < t <= 30.0, f"残り時間が制限に反映されていない: {t}"


def test_caller_timeout_is_not_exceeded():
    """呼び出し側が指定した制限（文章入力の60秒など）を延ばさないこと。"""
    fake = _FakeClient([_Resp([_Blk("text", "{}")])])
    m._create_with_server_tools(fake, deadline=time.monotonic() + 300.0, timeout=60.0,
                                messages=[{"role": "user", "content": "x"}])
    assert fake.messages.calls[0]["timeout"] == 60.0


def test_total_budget_fits_in_gunicorn_timeout():
    """サーバーの持ち時間が、gunicornの打ち切り(300秒)より十分に短いこと。
    超えるとワーカーごと落ち、同時に解析中の他の会員まで巻き添えで失敗する。"""
    assert m.RETRY_TIME_BUDGET_SEC + m.RETRY_RESERVE_SEC < 300.0
    render = _read("render.yaml")
    gunicorn_timeout = int(re.search(r"--timeout (\d+)", render).group(1))
    assert m.RETRY_TIME_BUDGET_SEC < gunicorn_timeout - 30
