# 文章入力（/analyze-text）の回帰テスト。
# 2026-07 に「文章で入力すると通信エラーになる」障害が起きたため、
# このエンドポイントがどんな状況でも必ずJSON（日本語メッセージ入り）を返すことを保証する。
#
# 実行方法:  pytest tests/ -q
# AIは呼ばない（create_and_parse をモックする）ので、APIキー不要・数秒で終わる。

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as m  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    m.app.config["TESTING"] = True
    # AIクライアントは存在する体にする（実呼び出しはモックで差し替える）
    monkeypatch.setattr(m, "get_client", lambda: object())
    with m.app.test_client() as c:
        yield c


SAMPLE_RESULT = {
    "foods": [
        {"name": "コーンフレーク", "category": "主食", "kcal_per_serving": 110,
         "amount": "半人前", "b_count": 0.5, "protein_g": 2, "veg_g": 0, "reason": "穀類"},
    ],
    "total_b_count": 0.5,
    "total_protein_g": 2,
    "total_veg_g": 0,
    "advice": "写真がないため推定は大まかです。",
}


def test_analyze_text_success(client, monkeypatch):
    """正常系：文章だけで解析結果のJSONが返る。"""
    monkeypatch.setattr(m, "create_and_parse", lambda *a, **k: dict(SAMPLE_RESULT))
    res = client.post("/analyze-text", data={"text": "コーンフレーク半人前", "user_id": "test-user"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["total_b_count"] == 0.5
    assert data["foods"][0]["name"] == "コーンフレーク"


def test_analyze_text_empty_text_returns_400_json(client):
    res = client.post("/analyze-text", data={"text": "  ", "user_id": "test-user"})
    assert res.status_code == 400
    assert "入力してください" in res.get_json()["error"]


def test_analyze_text_ai_empty_response_returns_502_json(client, monkeypatch):
    """AIが空応答を繰り返した場合でも、HTMLではなく日本語エラーJSONを返す。"""
    def _raise(*a, **k):
        raise m.EmptyAIResponse("retry-exhausted")
    monkeypatch.setattr(m, "create_and_parse", _raise)
    res = client.post("/analyze-text", data={"text": "カップラーメン1個", "user_id": "test-user"})
    assert res.status_code == 502
    assert res.is_json
    assert "もう一度お試しください" in res.get_json()["error"]


def test_analyze_text_unexpected_error_returns_500_json(client, monkeypatch):
    """想定外の例外でも必ずJSONで返す（クライアントが『通信エラー』と誤表示しないため）。"""
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(m, "create_and_parse", _boom)
    res = client.post("/analyze-text", data={"text": "おにぎり2個", "user_id": "test-user"})
    assert res.status_code == 500
    assert res.is_json
    assert "もう一度お試しください" in res.get_json()["error"]


def test_analyze_text_no_api_key_returns_401_json(client, monkeypatch):
    monkeypatch.setattr(m, "get_client", lambda: None)
    res = client.post("/analyze-text", data={"text": "おにぎり", "user_id": "test-user"})
    assert res.status_code == 401
    assert res.is_json


def test_analyze_text_passes_per_request_timeout(client, monkeypatch):
    """1回のAI試行に60秒の上限が指定されていること（スマホ側の通信タイムアウト対策）。"""
    captured = {}

    def _capture(_client, **kwargs):
        captured.update(kwargs)
        return dict(SAMPLE_RESULT)

    monkeypatch.setattr(m, "create_and_parse", _capture)
    res = client.post("/analyze-text", data={"text": "サラダ", "user_id": "test-user"})
    assert res.status_code == 200
    assert captured.get("timeout") == 60.0
