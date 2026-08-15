# 栄養解析のリトライ／エラー表示の回帰テスト。
# 背景（2026-08 オーナー指摘）：
#   カレーの写真をアップしたら進捗が0→100%になった後、もう一度0%から計算が始まり、
#   最後に「ただいま少し混み合っているようです」と表示された。
#   原因は ①サーバー側の再試行が2回×0.7秒しかなく過負荷(529)を吸収できない
#         ②クライアントが失敗時に analyzeItem を呼び直して進捗を0に戻していた
#   対策として、サーバー側で時間予算内に粘り、クライアントは進捗を戻さず静かに再送する。

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")

import anthropic  # noqa: E402
import httpx  # noqa: E402
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "templates", "index.html")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _overload_error(retry_after=None):
    """529（過負荷）のAPIエラーを模す（実APIは呼ばない）。"""
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    resp = httpx.Response(529, request=req, headers=headers)
    return anthropic.APIStatusError("overloaded", response=resp, body=None)


def _ok_response():
    """正常なAI応答を模す（content配列にテキストブロックを持つオブジェクト）。"""
    payload = json.dumps({
        "foods": [{"name": "カレー", "category": "B", "b_count": 1,
                   "protein_g": 10, "veg_g": 0}],
        "total_b_count": 1, "total_protein_g": 10, "total_veg_g": 0,
    }, ensure_ascii=False)
    block = type("Block", (), {"type": "text", "text": payload})()
    return type("Resp", (), {"content": [block], "stop_reason": "end_turn"})()


# ── サーバー側：過負荷を吸収できること ──────────────────────────
def test_retries_survive_repeated_overload(monkeypatch):
    """529が続いても、回復すれば結果を返せること（＝会員にエラーを出さない）。"""
    monkeypatch.setattr(m.time, "sleep", lambda s: None)  # テストは待たない
    calls = {"n": 0}

    def flaky(client, **kw):
        calls["n"] += 1
        if calls["n"] <= 3:          # 3回連続で過負荷
            raise _overload_error()
        return _ok_response()

    monkeypatch.setattr(m, "_create_with_server_tools", flaky)
    result = m.create_and_parse(object())
    assert result["foods"][0]["name"] == "カレー"
    assert calls["n"] == 4, f"過負荷で諦めています（呼び出し{calls['n']}回）"


def test_retry_count_is_more_than_two(monkeypatch):
    """再試行の上限が、以前の2回より増えていること。"""
    assert m.RETRY_MAX_ATTEMPTS >= 4, "過負荷を吸収するには再試行が足りません"
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_overloaded(client, **kw):
        calls["n"] += 1
        raise _overload_error()

    monkeypatch.setattr(m, "_create_with_server_tools", always_overloaded)
    try:
        m.create_and_parse(object())
    except m.EmptyAIResponse:
        pass
    assert calls["n"] >= 4, f"再試行が{calls['n']}回しかありません"


def test_retry_budget_fits_in_gunicorn_timeout():
    """再試行の総時間予算が、gunicornの--timeout(300秒)を超えないこと。
    超えるとワーカーが強制終了され500/502になり、かえって体験が悪化する。"""
    assert m.RETRY_TIME_BUDGET_SEC + m.RETRY_RESERVE_SEC <= 300


def test_retry_wait_honors_retry_after():
    """APIがRetry-Afterを返したらその秒数に従うこと。"""
    class _Resp:
        headers = {"retry-after": "3"}

    err = type("E", (), {"response": _Resp()})()
    assert m._retry_wait_seconds(err, 0) == 3.0
    # ヘッダが無ければ指数バックオフ（1秒前後 → 2秒前後 …）
    plain = m._retry_wait_seconds(None, 0)
    assert 1.0 <= plain < 1.5
    assert 2.0 <= m._retry_wait_seconds(None, 1) < 2.5


# ── 会員に見せる文言 ─────────────────────────────────────────
def test_busy_message_never_says_congested():
    """「混み合っています／時間をおいて再試行してください」とは出さないこと。"""
    msg = m.ANALYSIS_BUSY_MESSAGE
    assert "混み合" not in msg
    assert "時間をおいて" not in msg
    # 写真が保存されていて自動再開することを伝える
    assert "保存" in msg and "自動" in msg


def test_no_congestion_message_anywhere_user_facing():
    """サーバー・画面のどこにも旧「混み合っています」文言が残っていないこと。"""
    app_src = _read(os.path.join(ROOT, "app.py"))
    html = _read(INDEX)
    for src, name in ((app_src, "app.py"), (html, "index.html")):
        # 方針を説明するコメント行は除外して判定する
        for line in src.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            assert "ただいま少し混み合っている" not in line, f"{name} に旧文言が残っています: {line}"
            assert "AIサーバーが混み合っている" not in line, f"{name} に旧文言が残っています: {line}"


# ── 二段階解析（Haiku事前チェック）が復活していないこと ────────────
def test_precheck_endpoint_is_removed():
    """/precheck（Haikuの事前チェック）が削除されていること。"""
    app_src = _read(os.path.join(ROOT, "app.py"))
    assert '@app.route("/precheck"' not in app_src, "/precheck が復活しています（2回計算の原因）"
    assert "PRECHECK_PROMPT" not in app_src
    client = m.app.test_client()
    assert client.post("/precheck").status_code == 404


def test_client_has_no_precheck_or_reanalyze_helpers():
    """画面側にも事前チェック／確認後の再計算のコードが残っていないこと。"""
    html = _read(INDEX)
    for name in ("collectPrecheckAnswers", "collectClarifyAnswers", "reanalyzeWithAnswers"):
        assert name not in html, f"{name} が残っています（2段階解析の名残）"
    assert "'/precheck'" not in html


# ── クライアント：進捗を0に戻す再実行をしないこと ──────────────────
def test_client_retry_does_not_restart_progress():
    """analyzeItem が自分自身を呼び直して進捗を0%からやり直さないこと。"""
    html = _read(INDEX)
    start = html.index("async function analyzeItem(")
    end = html.index("// 文章入力（写真なし）の解析を再開する", start)
    body = html[start:end]
    assert "return analyzeItem(" not in body, \
        "失敗時に analyzeItem を呼び直しています（進捗が0%に戻り2回計算に見える）"
    # 進捗の開始は1回だけ
    assert body.count("_startProgress(") == 1
    # ループで静かに再送する実装になっていること
    assert "ANALYZE_CLIENT_RETRIES" in body
    assert "_retryable" in body


def test_retryable_flag_is_set_for_transient_errors():
    """一時的な状態コードだけを再送対象にしていること。"""
    html = _read(INDEX)
    assert "RETRYABLE_STATUS" in html
    # 413（サイズ過大）は再送しても直らないので対象外
    start = html.index("const RETRYABLE_STATUS")
    line = html[start:html.index("\n", start)]
    assert "413" not in line, "413は再送しても直らないので対象外にすること"
    for code in ("429", "500", "502", "503", "504"):
        assert code in line, f"{code} が再送対象に含まれていません"
