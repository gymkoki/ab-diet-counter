# ダッシュボード各会員ページ最上部のBMI表示に関する回帰テスト。
# オーナー指示 2026-07：BMIで減量幅も異なるので、各個人ページの一番上にBMIを出す。

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_USER = os.path.join(ROOT, "templates", "admin_user.html")


@pytest.fixture
def client():
    m.init_db()
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c


def test_profile_post_stores_height(client):
    """/api/profile に height_cm を送ると user_profile に保存される。"""
    uid = "test-bmi-1"
    r = client.post("/api/profile", json={"user_id": uid, "height_cm": 168})
    assert r.status_code == 200
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute(f"SELECT height_cm FROM user_profile WHERE user_id={m.PH}", (uid,))
        row = cur.fetchone(); conn.close()
    assert row and float(row[0]) == 168.0


def test_profile_post_ignores_invalid_height(client):
    """不正な身長（範囲外）は保存しない（既存値も壊さない）。"""
    uid = "test-bmi-2"
    client.post("/api/profile", json={"user_id": uid, "height_cm": 170})
    client.post("/api/profile", json={"user_id": uid, "height_cm": 5})  # 不正
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute(f"SELECT height_cm FROM user_profile WHERE user_id={m.PH}", (uid,))
        row = cur.fetchone(); conn.close()
    assert row and float(row[0]) == 170.0


def test_admin_detail_returns_height(client):
    """管理APIの detail が height_cm を返す。"""
    uid = "test-bmi-3"
    client.post("/api/profile", json={"user_id": uid, "height_cm": 175})
    r = client.get(f"/api/admin/user/{uid}/detail",
                   headers={"X-Admin-Password": m.ADMIN_PASSWORD})
    assert r.status_code == 200
    assert r.get_json().get("height_cm") == 175.0


def test_admin_user_html_has_bmi_card_and_logic():
    """会員ページにBMIカードとBMI算出ロジックがあること。"""
    with open(ADMIN_USER, encoding="utf-8") as f:
        html = f.read()
    assert 'id="bmi-card"' in html and 'id="bmi-value"' in html
    assert "function renderBMI" in html
    # 身長(cm)から m 換算してBMI=体重/身長^2 を計算している
    assert re.search(r"w\s*/\s*\(\(h\s*/\s*100\)\s*\*\*\s*2\)", html)
