# AIのAPIエラー（残高不足など）を会員向けの日本語メッセージに変換する回帰テスト。
# 2026-08：残高不足の英語エラー（Your credit balance is too low...）が
# 会員のアプリ画面にそのまま表示された障害の再発防止。

import os
import sys

import anthropic
import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402


def _api_error(message: str):
    """anthropic.APIError 相当の例外を作る（実APIは呼ばない）。"""
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIError(message, request=req, body=None)


@pytest.fixture
def client(monkeypatch):
    m.init_db()
    m.app.config["TESTING"] = True
    monkeypatch.setattr(m, "get_client", lambda: object())
    with m.app.test_client() as c:
        yield c


def test_credit_error_is_hidden_from_members():
    """残高不足の生エラーは会員に見せず、運営対応中の案内に変換する。"""
    raw = ("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
           "'message': 'Your credit balance is too low to access the Anthropic API. "
           "Please go to Plans & Billing to upgrade or purchase credits.'}}")
    msg = m._api_error_message(_api_error(raw), "TEST")
    assert "credit balance" not in msg
    assert "Billing" not in msg and "Error code" not in msg
    assert "運営" in msg and "お試しください" in msg


def test_generic_api_error_is_also_friendly():
    msg = m._api_error_message(_api_error("Error code: 500 - internal"), "TEST")
    assert "Error code" not in msg
    assert "もう一度お試しください" in msg


def test_analyze_text_credit_error_returns_friendly_json(client, monkeypatch):
    """/analyze-text が残高不足でも、会員向けの日本語JSONを返す。"""
    def boom(*a, **k):
        raise _api_error("Your credit balance is too low to access the Anthropic API.")

    monkeypatch.setattr(m, "create_and_parse", boom)
    res = client.post("/analyze-text", data={"text": "おにぎり", "user_id": "u1"})
    assert res.status_code == 503
    err = res.get_json()["error"]
    assert "credit balance" not in err and "運営" in err


def test_api_health_requires_admin(client):
    assert client.get("/api/admin/api-health").status_code == 404


def test_api_health_reports_no_key(client, monkeypatch):
    """APIキー未設定を運営に分かる形で返す（キー全体は返さない）。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    r = client.get("/api/admin/api-health", headers={"X-Admin-Password": m.ADMIN_PASSWORD})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is False and d["status"] == "no_key"


def test_api_health_detects_no_credit(client, monkeypatch):
    """残高不足を status=no_credit として運営に知らせる。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-dummy-key-for-test-1234")

    class _FakeMessages:
        def create(self, **kwargs):
            raise _api_error("Your credit balance is too low to access the Anthropic API.")

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(m.anthropic, "Anthropic", lambda **kw: _FakeClient())
    r = client.get("/api/admin/api-health", headers={"X-Admin-Password": m.ADMIN_PASSWORD})
    d = r.get_json()
    assert d["ok"] is False and d["status"] == "no_credit"
    # 運営には原因特定用にキーの一部だけ見せる（全体は出さない）
    assert d["key_hint"] and "…" in d["key_hint"]
    assert "sk-ant-api03-dummy-key-for-test-1234" not in str(d)
