"""デイリーレポートのいちばん下「💰 収益を伸ばすために今日やること」（オーナー指示 2026-10-24）の回帰テスト。

「アプリの収益を改善するために何をすべきかを毎日1〜3つ書いて。コピペできるように」。
  ・サーバー（/api/admin/revenue-proposals）が1日1回だけAIで作ってキャッシュする（二重にAI費用をかけない）
  ・AIに渡すのは集計した数字だけ（会員の名前・IDは渡さない）
  ・提案ごとに Claude Code にそのまま貼れる依頼文（prompt）を付ける。レポートには全部まとめた依頼文も置く
"""
import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SAMPLE = [
    {"title": "申し込み画面への導線", "why": "登録42名のうち有料は5名。記録画面から申し込みに進む入口が無い。",
     "effect": "月+5,500円（有料会員+5人）", "kind": "アプリ",
     "prompt": "記録画面の上に、まだ申し込んでいない会員だけに有料プランの案内カードを出してください。"},
    {"title": "ジムでの案内文", "why": "直近7日に記録した会員は18名。ジムで直接案内できる。",
     "effect": "月+3,300円（+3人）", "kind": "運営",
     "prompt": "ジムの受付に置く、有料プランの案内文（A4・1枚）の下書きを作ってください。"},
    {"title": "写真をまとめて撮る", "why": "解析30日で900回・費用3,600円。", "effect": "月−800円の費用削減",
     "kind": "よくわからない", "prompt": "写真を選ぶ画面に「1食1枚にまとめて」の一言を添えてください。"},
    {"title": "4つ目（捨てられる）", "why": "x", "effect": "x", "kind": "アプリ", "prompt": "x"},
]


class _Block:
    type = "text"

    def __init__(self, t):
        self.text = t


class _Msg:
    def __init__(self, t):
        self.content = [type("T", (), {"type": "thinking", "thinking": "…"})(), _Block(t)]


def _stub_ai(monkeypatch, payload_text, fail_models=()):
    calls = []

    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            if kw["model"] in fail_models:
                raise RuntimeError("model not available")
            return _Msg(payload_text)

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(m, "get_client", lambda: _Client())
    return calls


def _clear_cache():
    today = datetime.datetime.now(m.JST).date()
    for i in range(0, m.REVENUE_PROPOSAL_HISTORY_DAYS + 1):
        m._set_setting(f"revenue-proposals-{(today - datetime.timedelta(days=i)).isoformat()}", "")


@pytest.fixture
def client():
    m.init_db()
    m.app.config["TESTING"] = True
    _clear_cache()
    with m.app.test_client() as c:
        yield c
    _clear_cache()


def test_requires_admin(client):
    assert client.get("/api/admin/revenue-proposals").status_code == 404


def test_returns_up_to_three_with_copyable_prompt(client, monkeypatch):
    calls = _stub_ai(monkeypatch, json.dumps({"proposals": SAMPLE}, ensure_ascii=False))
    d = client.get("/api/admin/revenue-proposals", headers=ADMIN).get_json()
    assert len(d["proposals"]) == 3, "1〜3件に収めること"
    first = d["proposals"][0]
    assert first["prompt"] and first["title"] and first["effect"] and first["why"]
    assert d["proposals"][2]["kind"] == "アプリ", "kind は「アプリ」「運営」のどちらかにそろえる"
    assert d["cached"] is False and len(calls) == 1


def test_one_ai_call_per_day(client, monkeypatch):
    """朝の本送信とテスト送信で、同じ日にAIを2回呼ばない（費用を二重にかけない）。"""
    calls = _stub_ai(monkeypatch, json.dumps({"proposals": SAMPLE[:1]}, ensure_ascii=False))
    client.get("/api/admin/revenue-proposals", headers=ADMIN)
    r2 = client.get("/api/admin/revenue-proposals", headers=ADMIN).get_json()
    assert len(calls) == 1 and r2["cached"] is True


def test_falls_back_to_next_model(client, monkeypatch):
    calls = _stub_ai(monkeypatch, json.dumps({"proposals": SAMPLE[:2]}, ensure_ascii=False),
                     fail_models=(m.REVENUE_PROPOSAL_MODELS[0],))
    d = client.get("/api/admin/revenue-proposals", headers=ADMIN).get_json()
    assert len(d["proposals"]) == 2
    assert [c["model"] for c in calls] == list(m.REVENUE_PROPOSAL_MODELS[:2])


def test_broken_response_returns_502(client, monkeypatch):
    _stub_ai(monkeypatch, "JSONではありません")
    assert client.get("/api/admin/revenue-proposals", headers=ADMIN).status_code == 502


def test_ai_gets_numbers_but_no_member_names(client, monkeypatch):
    """AIに渡すのは集計値だけ。会員の名前・IDは渡さない。直近の提案の見出しは渡す（繰り返し防止）。"""
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM user_profile WHERE user_id={m.PH}", ("rev-secret-uid",))
    cur.execute(f"INSERT INTO user_profile (user_id, display_name, goal, updated_at) VALUES ({m.PH},{m.PH},'cut','x')",
                ("rev-secret-uid", "ひみつ花子"))
    conn.commit()
    conn.close()
    yesterday = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=1)).isoformat()
    m._set_setting(f"revenue-proposals-{yesterday}", json.dumps([{"title": "昨日の提案XYZ", "prompt": "p"}],
                                                                ensure_ascii=False))
    calls = _stub_ai(monkeypatch, json.dumps({"proposals": SAMPLE[:1]}, ensure_ascii=False))
    try:
        client.get("/api/admin/revenue-proposals", headers=ADMIN)
    finally:
        conn = m._get_conn()
        conn.cursor().execute(f"DELETE FROM user_profile WHERE user_id={m.PH}", ("rev-secret-uid",))
        conn.commit()
        conn.close()
    text = calls[0]["messages"][0]["content"][0]["text"]
    assert "ひみつ花子" not in text and "rev-secret-uid" not in text
    assert "registered_members" in text and "price_jpy" in text
    assert "昨日の提案XYZ" in text


def test_prompt_keeps_owner_policies():
    p = m.REVENUE_PROPOSAL_PROMPT
    assert "呼び戻し" in p, "離脱者の呼び戻しを禁止すること（オーナー方針 2026-09-09）"
    assert "解約しにくくする仕組み" in p and "誇大な減量効果" in p
    assert "既存の会員に無料期間は無い" in p, "オーナーが決めた料金方針を覆させない"
    assert "見込み効果" in p and "数合わせ" in p


def test_report_section_at_bottom_with_copy_text():
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report as sr
    rev = {"date": "2026-10-24", "proposals": [dict(p) for p in SAMPLE[:2]]}
    sec = sr._revenue_section(rev)
    assert "💰 収益を伸ばすために今日やること（2つ）" in sec
    assert "申し込み画面への導線" in sec and "月+5,500円" in sec
    assert SAMPLE[0]["prompt"] in sec, "依頼文をそのままコピーできること"
    assert "全部まとめて頼むとき" in sec and "2026-10-24 のデイリーレポート" in sec
    assert sr._revenue_section(None) == "" and sr._revenue_section({"proposals": []}) == ""
    assert "よくわからない" not in sr._revenue_section({"proposals": [SAMPLE[2]]}), "種類は「アプリ」「運営」だけ"
    assert "&lt;b&gt;" in sr._revenue_section({"proposals": [{"title": "<b>", "prompt": "p"}]}), "HTMLを逃がす"

    src = open(os.path.join(ROOT, "report", "send_report.py"), encoding="utf-8").read()
    body = src[src.index("def build_html("):]
    # いちばん下（④の後・フッターの前）に置く
    assert body.index("🎯 4. 減量希望者") < body.index("{_revenue_section(revenue)}") < body.index('<div class="footer">')
    assert "revenue-proposals" in src and "fetch_revenue_proposals()" in src


# ── 2026-10-25：レポートの欄が空になった件の再発防止 ─────────────────────
def _stub_by_model(monkeypatch, replies):
    """モデルごとに返す本文を変える（None は例外）。"""
    calls = []

    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            text = replies.get(kw["model"])
            if text is None:
                raise RuntimeError("model not available")
            msg = _Msg(text)
            msg.stop_reason = "max_tokens" if not text.strip().endswith("}") else "end_turn"
            return msg

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(m, "get_client", lambda: _Client())
    return calls


def test_truncated_reply_falls_back_to_next_model(client, monkeypatch):
    """1つ目のモデルの応答が途中切れ（考えすぎて上限に達した等）でも、2つ目で作り直す。
    以前はここで 502 になり、レポートの欄が空になっていた。"""
    good = json.dumps({"proposals": SAMPLE[:1]}, ensure_ascii=False)
    first, second = m.REVENUE_PROPOSAL_MODELS[:2]
    calls = _stub_by_model(monkeypatch, {first: '{"proposals":[{"title":"途中', second: good})
    r = client.get("/api/admin/revenue-proposals", headers=ADMIN)
    assert r.status_code == 200 and len(r.get_json()["proposals"]) == 1
    assert [c["model"] for c in calls] == [first, second]


def test_enough_room_for_thinking(client, monkeypatch):
    calls = _stub_ai(monkeypatch, json.dumps({"proposals": SAMPLE[:1]}, ensure_ascii=False))
    client.get("/api/admin/revenue-proposals", headers=ADMIN)
    assert calls[0]["max_tokens"] >= 16000, "思考の分も含めて余裕を持たせる（8000では途中で切れた）"


def test_failure_reason_is_returned_and_kept(client, monkeypatch):
    _stub_by_model(monkeypatch, {})
    m._set_setting(m.REVENUE_PROPOSAL_ERROR_KEY, "")
    r = client.get("/api/admin/revenue-proposals", headers=ADMIN)
    assert r.status_code == 502
    assert "RuntimeError" in r.get_json()["detail"]
    assert "RuntimeError" in json.loads(m._get_setting(m.REVENUE_PROPOSAL_ERROR_KEY, "{}"))["reason"]


def test_points_ranking_names_are_not_sent_to_ai(client, monkeypatch):
    """こまめポイントの上位5人（名前つき）はAIに渡さない。"""
    yesterday = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=1)).isoformat()
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM user_profile WHERE user_id={m.PH}", ("rev-top-uid",))
    cur.execute(f"DELETE FROM meal_points WHERE user_id={m.PH}", ("rev-top-uid",))
    cur.execute(f"INSERT INTO user_profile (user_id, display_name, goal, updated_at) VALUES ({m.PH},{m.PH},'cut','x')",
                ("rev-top-uid", "ランキング太郎"))
    cur.execute(f"INSERT INTO meal_points (user_id, date, created_at) VALUES ({m.PH},{m.PH},'x')",
                ("rev-top-uid", yesterday))
    conn.commit()
    conn.close()
    calls = _stub_ai(monkeypatch, json.dumps({"proposals": SAMPLE[:1]}, ensure_ascii=False))
    try:
        client.get("/api/admin/revenue-proposals", headers=ADMIN)
    finally:
        conn = m._get_conn()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM user_profile WHERE user_id={m.PH}", ("rev-top-uid",))
        cur.execute(f"DELETE FROM meal_points WHERE user_id={m.PH}", ("rev-top-uid",))
        conn.commit()
        conn.close()
    text = calls[0]["messages"][0]["content"][0]["text"]
    assert "mame_points" in text, "人数などの集計は渡す"
    assert "ランキング太郎" not in text and "rev-top" not in text


def test_metrics_with_decimal_do_not_break(client, monkeypatch):
    """PostgreSQL が返す Decimal などが混ざっても、AIへの入力を作れること。"""
    import decimal
    real = m._collect_revenue_metrics
    monkeypatch.setattr(m, "_collect_revenue_metrics", lambda: {**real(), "x": decimal.Decimal("1.5"),
                                                                "d": datetime.date(2026, 10, 25)})
    _stub_ai(monkeypatch, json.dumps({"proposals": SAMPLE[:1]}, ensure_ascii=False))
    assert client.get("/api/admin/revenue-proposals", headers=ADMIN).status_code == 200


def test_report_log_shows_status_and_reason(monkeypatch, capsys):
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report as sr

    class _R:
        ok, status_code = False, 502

        def json(self):
            return {"error": "x", "detail": "ValueError: AIの応答からJSONを取得できませんでした（stop_reason=max_tokens）"}
    monkeypatch.setattr(sr.requests, "get", lambda *a, **k: _R())
    assert sr.fetch_revenue_proposals() is None
    out = capsys.readouterr().out
    assert "HTTP 502" in out and "stop_reason=max_tokens" in out


# ── 2026-10-26：理由の無い HTTP 500 になった件 ──────────────────────────
def test_logging_failure_does_not_turn_into_500(client, monkeypatch):
    """標準出力が日本語を書けない環境でも、ログ出力で例外を出して 500 にしない（502＋理由を返す）。"""
    import builtins
    real_print = builtins.print

    def ascii_only_print(*args, **kw):
        text = " ".join(str(a) for a in args)
        text.encode("ascii")          # 日本語があれば UnicodeEncodeError
        real_print(*args, **kw)
    monkeypatch.setattr(builtins, "print", ascii_only_print)
    _stub_ai(monkeypatch, "JSONではありません")          # 日本語のエラーメッセージになる
    r = client.get("/api/admin/revenue-proposals", headers=ADMIN)
    assert r.status_code == 502, "ログ出力の失敗で理由の無い 500 になっている"
    assert r.get_json()["detail"]


def test_unprintable_exception_still_gives_reason(client, monkeypatch):
    class Weird(Exception):
        def __str__(self):
            raise RuntimeError("cannot str")

    class _Messages:
        def create(self, **kw):
            raise Weird()

    class _Client:
        messages = _Messages()
    monkeypatch.setattr(m, "get_client", lambda: _Client())
    r = client.get("/api/admin/revenue-proposals", headers=ADMIN)
    assert r.status_code == 502 and "Weird" in r.get_json()["detail"]


def test_report_log_shows_body_when_no_reason(monkeypatch, capsys):
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report as sr

    class _R:
        ok, status_code, text = False, 500, "<html><title>Internal Server Error</title></html>"

        def json(self):
            raise ValueError("not json")
    monkeypatch.setattr(sr.requests, "get", lambda *a, **k: _R())
    sr.fetch_revenue_proposals()
    out = capsys.readouterr().out
    assert "HTTP 500" in out and "Internal Server Error" in out


def test_mail_failure_is_annotated_with_the_fix():
    """メールが送れなかったとき、GitHub の実行画面に「やること」を赤い注記で出す。"""
    src = open(os.path.join(ROOT, "report", "send_report.py"), encoding="utf-8").read()
    assert "::error title=レポートのメールを送れませんでした" in src
    assert "アプリパスワードを入れ直す" in src


# ── 2026-10-28：提案は別スレッドで作る（サーバーそのものの 500 で理由も残らなかった件） ────
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402


def _wait_idle():
    for _ in range(100):
        if not m._revenue_generation_running(m._revenue_today()):
            return
        _time.sleep(0.05)


def test_report_data_starts_generation_in_background(client, monkeypatch):
    """レポートの集計を取りに来た時点で、提案づくりを別スレッドで始めておく。"""
    monkeypatch.setattr(m, "REVENUE_PROPOSAL_AUTOSTART", True)
    calls = _stub_ai(monkeypatch, json.dumps({"proposals": SAMPLE[:1]}, ensure_ascii=False))
    client.get("/api/admin/report-data", headers=ADMIN)
    _wait_idle()
    assert len(calls) == 1
    d = client.get("/api/admin/revenue-proposals", headers=ADMIN).get_json()
    assert d["cached"] is True and len(d["proposals"]) == 1, "あとから取りに来たときはすぐ返す"


def test_slow_generation_returns_202_then_result(client, monkeypatch):
    """作成中は 202 を返し、できたら 200。1回の問い合わせが長く待たされない。"""
    gate = _threading.Event()
    calls = []

    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            gate.wait(5)
            return _Msg(json.dumps({"proposals": SAMPLE[:2]}, ensure_ascii=False))

    class _Client:
        messages = _Messages()
    monkeypatch.setattr(m, "get_client", lambda: _Client())
    t0 = _time.monotonic()
    r = client.get("/api/admin/revenue-proposals?wait=0.3", headers=ADMIN)
    assert r.status_code == 202 and r.get_json()["status"] == "generating"
    assert _time.monotonic() - t0 < 3, "AIを待って長いリクエストにしない"
    r2 = client.get("/api/admin/revenue-proposals?wait=0.3", headers=ADMIN)
    assert r2.status_code == 202 and len(calls) == 1, "作成中に重ねてAIを呼ばない"
    gate.set()
    _wait_idle()
    r3 = client.get("/api/admin/revenue-proposals", headers=ADMIN)
    assert r3.status_code == 200 and len(r3.get_json()["proposals"]) == 2


def test_any_failure_in_background_is_recorded(client, monkeypatch):
    """別スレッドの中の失敗は、種類を問わず理由を残して 502 で返す（サーバーの 500 にしない）。"""
    class _Messages:
        def create(self, **kw):
            raise SystemExit("worker is stopping")       # Exception ではない種類の失敗

    class _Client:
        messages = _Messages()
    monkeypatch.setattr(m, "get_client", lambda: _Client())
    r = client.get("/api/admin/revenue-proposals", headers=ADMIN)
    assert r.status_code == 502 and "SystemExit" in r.get_json()["detail"]


def test_no_api_key_is_503_with_reason(client, monkeypatch):
    monkeypatch.setattr(m, "get_client", lambda: None)
    r = client.get("/api/admin/revenue-proposals", headers=ADMIN)
    assert r.status_code == 503 and "APIキー" in r.get_json()["detail"]


def test_report_polls_while_generating(monkeypatch):
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report as sr
    replies = [202, 202, 200]

    class _R:
        def __init__(self, code):
            self.status_code, self.ok, self.text = code, code < 400, ""

        def json(self):
            return {"status": "generating"} if self.status_code == 202 else {"date": "d", "proposals": SAMPLE[:1]}
    seen = []

    def fake_get(url, **kw):
        seen.append(kw.get("params"))
        return _R(replies.pop(0))
    monkeypatch.setattr(sr.requests, "get", fake_get)
    d = sr.fetch_revenue_proposals()
    assert d["proposals"] and len(seen) == 3 and seen[0] == {"wait": 20}
