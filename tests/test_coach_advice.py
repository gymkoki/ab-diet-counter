# AI減量コーチ（/api/admin/coach-advice）の回帰テスト。
# オーナー指示 2026-08：減量希望者が1か月で1kg減量できる工夫を、
# デイリーレポートのデータをもとに毎日提案してほしい（届け先はメール）。

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    m.init_db()
    m.app.config["TESTING"] = True
    monkeypatch.setattr(m, "get_client", lambda: object())
    # 日次キャッシュを毎テスト空にする
    today = datetime.datetime.now(m.JST).strftime("%Y-%m-%d")
    m._set_setting(f"coach-advice-{today}", "")
    with m.app.test_client() as c:
        yield c


def _seed_cut_member(uid="coach-test-1", name="テスト会員"):
    ts = "2026-08-01T08:00:00"
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute(f"DELETE FROM user_profile WHERE user_id={m.PH}", (uid,))
        cur.execute(
            f"INSERT INTO user_profile (user_id, display_name, goal, updated_at) VALUES ({m.PH},{m.PH},'cut',{m.PH})",
            (uid, name, ts))
        conn.commit(); conn.close()


def test_coach_advice_requires_admin(client):
    res = client.get("/api/admin/coach-advice")
    assert res.status_code == 404   # 未認証は404（管理画面の存在を隠す方針）


def test_coach_advice_generates_and_caches(client, monkeypatch):
    _seed_cut_member()
    calls = {"n": 0}

    def fake_ai(client_obj, user_text):
        calls["n"] += 1
        assert "テスト会員" in user_text or "coach-te" in user_text
        return "■ 全体の状況\nテスト提案です。"

    monkeypatch.setattr(m, "_call_coach_ai", fake_ai)
    headers = {"X-Admin-Password": m.ADMIN_PASSWORD}

    r1 = client.get("/api/admin/coach-advice", headers=headers)
    assert r1.status_code == 200
    d1 = r1.get_json()
    assert "テスト提案" in d1["advice"] and d1["cached"] is False

    # 2回目はキャッシュから返し、AIを再度呼ばない（テスト送信＋朝の本送信の二重課金防止）
    r2 = client.get("/api/admin/coach-advice", headers=headers)
    assert r2.get_json()["cached"] is True
    assert calls["n"] == 1


def test_coach_advice_ai_failure_returns_json_error(client, monkeypatch):
    _seed_cut_member()

    def boom(client_obj, user_text):
        raise RuntimeError("ai down")

    monkeypatch.setattr(m, "_call_coach_ai", boom)
    r = client.get("/api/admin/coach-advice", headers={"X-Admin-Password": m.ADMIN_PASSWORD})
    assert r.status_code == 502
    assert r.is_json and "再試行" in r.get_json()["error"]


def test_report_script_includes_coach_section():
    """メールレポート側に提案セクションが組み込まれていること。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "report", "send_report.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "fetch_coach_advice" in src
    assert "coach-advice" in src
    assert "AI減量コーチ" in src
    # 提案が取れない日でもレポート本体は送る（return None で握りつぶす設計）
    assert "return None" in src
