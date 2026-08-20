"""クレジット残高の「基準」をアプリ側に登録するAPIの回帰テスト。

オーナー指示 2026-08：残高が取得できないときも「予測でよいので、だいたい
あとどれくらい残っているか」を出す。そのためには基準となる残高が要るが、
GitHub Secrets を触ってもらうのは負担が大きいので、管理画面から登録できるようにした。
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}


@pytest.fixture
def client():
    m.init_db()
    m.app.config["TESTING"] = True
    m._set_setting("credit_base_date", "")
    m._set_setting("credit_base_usd", "")
    with m.app.test_client() as c:
        yield c


def test_requires_admin(client):
    assert client.get("/api/admin/credit-base").status_code == 404
    assert client.post("/api/admin/credit-base", json={}).status_code == 404
    assert client.get("/api/admin/credit-estimate").status_code == 404


def test_save_and_read_back(client):
    r = client.post("/api/admin/credit-base",
                    json={"date": "2026-08-01", "amount_usd": "47.20"}, headers=ADMIN)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    d = client.get("/api/admin/credit-base", headers=ADMIN).get_json()
    assert d["base_date"] == "2026-08-01"
    assert d["base_usd"] == pytest.approx(47.20)


def test_accepts_dollar_sign_and_commas(client):
    r = client.post("/api/admin/credit-base",
                    json={"date": "2026-08-01", "amount_usd": "$1,234.50"}, headers=ADMIN)
    assert r.status_code == 200
    assert client.get("/api/admin/credit-base", headers=ADMIN).get_json()["base_usd"] == pytest.approx(1234.50)


@pytest.mark.parametrize("payload", [
    {"date": "2026-13-99", "amount_usd": "10"},
    {"date": "", "amount_usd": "10"},
    {"date": "2026-08-01", "amount_usd": "abc"},
    {"date": "2026-08-01", "amount_usd": "-5"},
    {"date": "2026-08-01"},
])
def test_rejects_bad_input(client, payload):
    r = client.post("/api/admin/credit-base", json=payload, headers=ADMIN)
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_unset_base_reads_as_none(client):
    d = client.get("/api/admin/credit-base", headers=ADMIN).get_json()
    assert d["base_date"] is None and d["base_usd"] is None


def test_estimate_returns_daily_cost_from_analysis_count(client):
    """解析回数 × 1回あたりの概算コスト が日別に返ること。"""
    today = datetime.datetime.now(m.JST).date().isoformat()
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM usage_log")
        for _ in range(3):
            cur.execute(f"INSERT INTO usage_log (user_id, created_at) VALUES ({m.PH},{m.PH})",
                        ("u-1", f"{today}T10:00:00"))
        conn.commit()
    finally:
        conn.close()

    d = client.get("/api/admin/credit-estimate", headers=ADMIN).get_json()
    assert d["total_analyses"] == 3
    assert d["daily_jpy"][today] == pytest.approx(3 * m.COST_PER_ANALYSIS_JPY)
    assert d["cost_per_analysis_jpy"] == m.COST_PER_ANALYSIS_JPY


def test_estimate_includes_registered_base(client):
    client.post("/api/admin/credit-base",
                json={"date": "2026-08-01", "amount_usd": "80"}, headers=ADMIN)
    d = client.get("/api/admin/credit-estimate", headers=ADMIN).get_json()
    assert d["base_date"] == "2026-08-01" and d["base_usd"] == pytest.approx(80.0)


def test_estimate_reaches_back_to_base_date(client):
    """基準日が既定のさかのぼり日数より古くても、そこまで遡って集計すること。"""
    old = (datetime.datetime.now(m.JST).date()
           - datetime.timedelta(days=m.CREDIT_ESTIMATE_MIN_DAYS + 40)).isoformat()
    client.post("/api/admin/credit-base", json={"date": old, "amount_usd": "80"}, headers=ADMIN)
    d = client.get("/api/admin/credit-estimate", headers=ADMIN).get_json()
    assert d["since"] <= old, "基準日より前から集計していません（使用額を取りこぼします）"
