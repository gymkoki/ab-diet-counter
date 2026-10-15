"""3食こまめ記録ポイント（オーナー指示 2026-10-15）の回帰テスト。

・朝・昼・夜の3回とも、その時間帯のうちに記録できた日に1ポイント（1日1ポイントまで）
・10ポイントたまると「奥松の特別メッセージ」（動画URL）が見られる
・動画URLは管理画面から後日登録する（未登録なら「準備中」）
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

import app as m

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
INDEX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "index.html")


@pytest.fixture
def client():
    m.init_db()
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM meal_points")
    cur.execute(f"DELETE FROM settings WHERE key={m.PH}", (m.SPECIAL_MESSAGE_URL_KEY,))
    conn.commit()
    conn.close()
    yield m.app.test_client()


def _seed(uid, n):
    conn = m._get_conn()
    cur = conn.cursor()
    today = datetime.date(2026, 1, 1)
    for i in range(n):
        cur.execute(f"INSERT INTO meal_points (user_id,date,created_at) VALUES ({m.PH},{m.PH},'x')",
                    (uid, (today + datetime.timedelta(days=i)).isoformat()))
    conn.commit()
    conn.close()


def _today():
    return datetime.datetime.now(m.JST).date().isoformat()


def test_one_point_per_day(client):
    for _ in range(3):
        r = client.post("/api/meal-points", json={"user_id": "u1", "date": _today()})
        assert r.status_code == 200
    assert r.get_json()["points"] == 1, "同じ日を何度送っても1ポイントだけ"


def test_cannot_claim_old_days(client):
    old = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=5)).isoformat()
    r = client.post("/api/meal-points", json={"user_id": "u1", "date": old})
    assert r.status_code == 400
    assert client.get("/api/meal-points?user_id=u1").get_json()["points"] == 0


def test_video_url_only_after_ten_points(client):
    client.post("/api/admin/special-message", json={"url": "https://youtu.be/abcdefghijk"}, headers=ADMIN)
    _seed("u9", 9)
    d = client.get("/api/meal-points?user_id=u9").get_json()
    assert d["points"] == 9 and d["unlocked"] is False and d["message_url"] is None
    assert d["message_ready"] is True
    client.post("/api/meal-points", json={"user_id": "u9", "date": _today()})
    d = client.get("/api/meal-points?user_id=u9").get_json()
    assert d["points"] == 10 and d["unlocked"] is True
    assert d["message_url"] == "https://youtu.be/abcdefghijk"


def test_unlocked_without_video_says_not_ready(client):
    _seed("u10", 10)
    d = client.get("/api/meal-points?user_id=u10").get_json()
    assert d["unlocked"] is True and d["message_ready"] is False and not d["message_url"]


def test_admin_rejects_non_https_and_is_protected(client):
    assert client.post("/api/admin/special-message", json={"url": "https://x.y"}).status_code in (401, 403, 404)
    r = client.post("/api/admin/special-message", json={"url": "javascript:alert(1)"}, headers=ADMIN)
    assert r.status_code == 400
    _seed("a", 10); _seed("b", 3)
    d = client.get("/api/admin/special-message", headers=ADMIN).get_json()
    assert d["members_with_points"] == 2 and d["members_unlocked"] == 1


# ── 端末側：時間帯の判定（実物の itemRecordedSlot を Node で動かす） ──
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node が無い")


def _slot(jst_str):
    html = open(INDEX, encoding="utf-8").read()
    slots = re.search(r"const MEAL_POINT_SLOTS = \[.*?\];", html, re.S).group(0)
    fn = re.search(r"function itemRecordedSlot\(item\) \{.*?\n\}", html, re.S).group(0)
    ms = int(datetime.datetime.fromisoformat(jst_str + "+09:00").timestamp() * 1000)
    js = f"{slots}\n{fn}\nconsole.log(JSON.stringify(itemRecordedSlot({{id: {ms} * 100 + 3}})));"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
    try:
        out = subprocess.run(["node", f.name], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    finally:
        os.unlink(f.name)


@needs_node
@pytest.mark.parametrize("t,expected", [
    ("2026-10-15T07:30:00", "morning"),
    ("2026-10-15T10:29:00", "morning"),
    ("2026-10-15T10:30:00", "noon"),
    ("2026-10-15T15:59:00", "noon"),
    ("2026-10-15T16:00:00", "night"),
    ("2026-10-15T23:50:00", "night"),
])
def test_slot_by_recorded_time(t, expected):
    r = _slot(t)
    assert r["slot"] == expected and r["date"] == t[:10]


@needs_node
def test_late_night_does_not_count():
    assert _slot("2026-10-16T02:00:00") is None


def test_only_today_records_count():
    """過去の日に後から足した記録・別の日の記録で、今日のポイントが付かないこと。"""
    html = open(INDEX, encoding="utf-8").read()
    fn = re.search(r"function todayMealPointSlots\(\) \{.*?\n\}", html, re.S).group(0)
    assert "r.date === today" in fn and "currentDate() !== today" in fn


def test_card_is_on_record_screen():
    html = open(INDEX, encoding="utf-8").read()
    assert 'id="meal-points"' in html and "奥松の特別メッセージ" in html
    assert "renderMealPoints();" in re.search(r"function updateDailyTotal\(\) \{.*?\n\}", html, re.S).group(0)
