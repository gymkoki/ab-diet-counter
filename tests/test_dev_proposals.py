"""毎朝の「Claude Code に頼める改善案」（/api/admin/dev-proposals）の回帰テスト。

オーナー指示 2026-08：デイリーレポートの内容を踏まえて、アプリをどう直せるかを毎朝提案してほしい。
手間をかけずに実行できるよう、提案は「そのままコピペできる依頼文」として届く。
"""
import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}

SAMPLE = [
    {"title": "記録再開のひと押し", "why": "31日以上記録が無い会員が3名。復帰の入口が無い。",
     "effort": "小", "prompt": "しばらく記録が無い会員がアプリを開いたとき、体重だけを入力できる小さなカードをホームに出してください。"},
    {"title": "Bカウントの見える化", "why": "増加傾向の会員はBが高止まり。今日の残りが分かりづらい。",
     "effort": "中", "prompt": "ホームに「今日の残りBカウント」を大きく表示してください。"},
    {"title": "週次のふり返り", "why": "週単位の変化を見る画面が無い。", "effort": "大",
     "prompt": "履歴タブに週ごとのサマリーを追加してください。"},
]


def _stub_ai(payload_text):
    class _Block:
        type = "text"

        def __init__(self, t):
            self.text = t

    class _Msg:
        def __init__(self, t):
            self.content = [_Block(t)]

    class _Messages:
        calls = 0

        def create(self, **kw):
            _Messages.calls += 1
            return _Msg(payload_text)

    class _Client:
        messages = _Messages()

    m.get_client = lambda: _Client()
    return _Client.messages


@pytest.fixture
def client():
    m.init_db()
    m.app.config["TESTING"] = True
    # 当日キャッシュを空にする
    today = datetime.datetime.now(m.JST).strftime("%Y-%m-%d")
    m._set_setting(f"dev-proposals-{today}", "")
    with m.app.test_client() as c:
        yield c


def test_requires_admin(client):
    """未認証は404（管理画面の存在を隠す方針）。"""
    assert client.get("/api/admin/dev-proposals").status_code == 404


def test_returns_three_proposals_with_copyable_prompt(client):
    msgs = _stub_ai(json.dumps({"proposals": SAMPLE}, ensure_ascii=False))
    r = client.get("/api/admin/dev-proposals", headers=ADMIN)
    assert r.status_code == 200
    d = r.get_json()
    assert len(d["proposals"]) == 3
    first = d["proposals"][0]
    # コピペして依頼できる文が必ず入っていること（これが無いと手間が増える）
    assert first["prompt"] and first["title"] and first["effort"] in ("小", "中", "大")
    assert d["cached"] is False
    assert msgs.calls >= 1


def test_second_call_uses_daily_cache(client):
    """同じ日に2回呼んでもAIは1回だけ（朝の本送信とテスト送信で二重課金しない）。"""
    msgs = _stub_ai(json.dumps({"proposals": SAMPLE}, ensure_ascii=False))
    before = msgs.calls
    client.get("/api/admin/dev-proposals", headers=ADMIN)
    after_first = msgs.calls
    r2 = client.get("/api/admin/dev-proposals", headers=ADMIN)
    assert msgs.calls == after_first, "キャッシュが効かずAIを呼び直しています"
    assert after_first == before + 1
    assert r2.get_json()["cached"] is True


def test_extra_text_around_json_is_tolerated(client):
    """AIが前置きを付けてもJSONを取り出せること。"""
    _stub_ai("はい、承知しました。\n" + json.dumps({"proposals": SAMPLE[:1]}, ensure_ascii=False))
    d = client.get("/api/admin/dev-proposals", headers=ADMIN).get_json()
    assert len(d["proposals"]) == 1


def test_broken_response_returns_502(client):
    """AIの応答が壊れていても500で落とさず、レポート側で握れる形にする。"""
    _stub_ai("JSONではありません")
    assert client.get("/api/admin/dev-proposals", headers=ADMIN).status_code == 502


def test_prompt_tells_ai_to_output_implementable_changes():
    """提案が「運用でがんばる」ではなく実装可能な変更になるよう指示していること。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app.py"), encoding="utf-8").read()
    assert "そのままコピー" in src
    assert "アプリの機能・画面・文言をどう変えるか" in src
    # 既にある機能を作り直す提案を防ぐため、現状の機能一覧を渡していること
    assert "APP_CAPABILITIES" in src
    assert "すでにある機能の焼き直しを提案しない" in src


def test_report_section_renders():
    """レポートメールに、コピペ用の枠つきで3件出ること。"""
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
    import send_report

    html = send_report._dev_proposal_section(SAMPLE)
    assert "Claude Code に頼める改善案" in html
    for p in SAMPLE:
        assert p["title"] in html
        assert p["prompt"] in html
    assert "コピーして Claude Code に貼るだけ" in html
    # 提案が無い日はセクションごと出さない
    assert send_report._dev_proposal_section([]) == ""
    assert send_report._dev_proposal_section(None) == ""


def test_report_survives_when_proposals_fail(monkeypatch):
    """改善案が取れなくてもレポート本体は送る（セクションだけ省略）。"""
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
    import send_report

    def _boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(send_report.requests, "get", _boom)
    monkeypatch.setattr(send_report.time, "sleep", lambda *_: None)
    assert send_report.fetch_dev_proposals() == []
