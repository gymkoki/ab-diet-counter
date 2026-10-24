"""こまめ記録ポイント（オーナー指示 2026-10-15。旧称「3食こまめ記録ポイント」）の回帰テスト。

・朝・昼・夜の3回とも、その時間帯のうちに記録できた日に1ポイント（1日1ポイントまで）
・10ポイント貯まると「リオールジムからの特別メッセージ」（動画URL）が見られる
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
    assert 'id="meal-points"' in html and "リオールジムからの特別メッセージ" in html
    assert "renderMealPoints();" in re.search(r"function updateDailyTotal\(\) \{.*?\n\}", html, re.S).group(0)


# ── デイリーレポート：昨日こまめポイントを獲得した人数（オーナー指示 2026-10-17。10-24 に「まめ」→「こまめ」） ──
def test_report_stats_count_yesterday(client):
    conn = m._get_conn()
    cur = conn.cursor()
    rows = [("a", "2026-10-16"), ("b", "2026-10-16"), ("c", "2026-10-15"), ("a", "2026-10-15")]
    for uid, d in rows:
        cur.execute(f"INSERT INTO meal_points (user_id,date,created_at) VALUES ({m.PH},{m.PH},'x')", (uid, d))
    conn.commit()
    conn.close()
    _seed("z", 10)
    st = m._mame_points_stats("2026-10-16")
    assert st["earned"] == 2
    assert st["trend"][-1] == {"date": "2026-10-16", "earned": 2}
    assert st["trend"][-2] == {"date": "2026-10-15", "earned": 2}
    assert len(st["trend"]) == 7
    assert st["unlocked"] == 1 and st["holders"] == 4


def test_report_data_includes_mame_points(client):
    d = client.get("/api/admin/report-data", headers=ADMIN).get_json()
    assert "mame_points" in d and "earned" in d["mame_points"]


def test_report_shows_mame_points_section():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
    sr = pytest.importorskip("send_report")
    html = sr._mame_points_section({"date": "2026-10-16", "earned": 3, "goal": 10, "unlocked": 1, "holders": 5,
                                    "trend": [{"date": "2026-10-16", "earned": 3}]}, recorders=12)
    assert "こまめポイント" in html and "3人" in html and "記録した12人中・25%" in html
    assert "まめポイント" not in html.replace("こまめポイント", ""), "旧称「まめポイント」が残っています"
    assert sr._mame_points_section(None) == ""
    src = open(sr.__file__, encoding="utf-8").read()
    assert '_mame_points_section(data.get("mame_points")' in src


def test_card_wording():
    """オーナー指示 2026-10-18：名前は「こまめ記録ポイント」（「3食」を付けない）、
    10ポイントの案内は「リオールジムから特別メッセージが見られます」（「奥松の」にしない）。"""
    html = open(INDEX, encoding="utf-8").read()
    assert '<span class="mpt-title">🎁 こまめ記録ポイント</span>' in html
    assert "ポイント貯まると、リオールジムから特別メッセージが見られます！" in html
    shown = re.sub(r"<!--.*?-->|/\*.*?\*/|//[^\n]*", "", html, flags=re.S)
    assert "3食こまめ記録ポイント" not in shown, "画面に「3食こまめ記録ポイント」が残っている"
    assert "奥松の特別メッセージ" not in shown, "画面に「奥松の特別メッセージ」が残っている"


def test_card_is_compact_and_below_record_buttons():
    """オーナー指示 2026-10-19：
    ・「写真を追加」をいちばん上に置き、ポイントのカードはその下（ボタンの下の置き場所へ移す）
    ・カードは1〜2行（見出し＋今日の朝・昼・夜）。ルールの説明は「？」を押したときだけ出す
    ・「文章で手動入力」「コピーご飯を追加」は点線の枠にしない"""
    html = open(INDEX, encoding="utf-8").read()
    build = re.search(r"function buildPanel\(m\) \{.*?\n\}", html, re.S).group(0)
    assert build.index('class="rec-grid"') < build.index('id="mpt-slot-${m.id}"'), "ポイントが写真ボタンより上にある"
    assert "mptSlot.appendChild(mptBox)" in build
    # 作り直しで消えないよう、innerHTML の前にカードを外へ戻している
    assert build.index("panel.contains(mptBox)") < build.index("panel.innerHTML")
    # 説明は既定で隠し、「？」で開く
    assert re.search(r"\.mpt \.mpt-msg \{ display:none;", html)
    assert ".mpt.help-open .mpt-msg { display:block; }" in html
    assert 'id="mpt-help"' in html and "function toggleMealPointsHelp()" in html
    assert 'id="mpt-stars"' not in html, "10個の目盛りの行が残っている（2行に縮める）"
    # 点線の枠をやめた
    rec_card = re.search(r"\n    \.rec-card \{[^}]*\}", html).group(0)
    assert "dashed" not in rec_card


def test_report_top5(client):
    """累計ポイントの上位5人（同点は同じ順位・直近7日に多い人を上に）。"""
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO user_profile (user_id, display_name, updated_at) VALUES ('p1','佐藤','x')")
    cur.execute(f"INSERT INTO user_profile (user_id, display_name, updated_at) VALUES ('p2','<b>鈴木</b>','x')")
    conn.commit()
    conn.close()
    _seed("p1", 8)          # 2026-01-01〜（直近7日には入らない）
    _seed("p2", 8)
    _seed("p3", 3)
    _seed("p4", 2); _seed("p5", 1); _seed("p6", 1)
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO meal_points (user_id,date,created_at) VALUES ('p2','2026-10-16','x')")
    cur.execute(f"INSERT INTO meal_points (user_id,date,created_at) VALUES ('p1','2026-10-15','x')")
    conn.commit()
    conn.close()
    top = m._mame_points_stats("2026-10-16")["top"]
    assert len(top) == 5
    assert [t["points"] for t in top] == [9, 9, 3, 2, 1]
    assert [t["rank"] for t in top] == [1, 1, 3, 4, 5], "同点は同じ順位"
    # 同点の2人は、最後に獲得した日が新しい p2（鈴木）が先
    assert top[0]["name"] == "<b>鈴木</b>" and top[1]["name"] == "佐藤"
    assert top[2]["name"] == "会員p3", "名前が無い会員は「会員xxxx」"


def test_report_top5_is_escaped():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
    sr = pytest.importorskip("send_report")
    html = sr._mame_points_section({"date": "2026-10-16", "earned": 1, "goal": 10, "unlocked": 0, "holders": 1,
                                    "trend": [], "top": [{"rank": 1, "name": "<b>鈴木</b>", "points": 9, "week": 1}]})
    assert "上位5人" in html and "&lt;b&gt;鈴木" in html and "<b>鈴木</b>" not in html
    assert "🥇1位" in html and "9pt" in html
