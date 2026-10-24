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
