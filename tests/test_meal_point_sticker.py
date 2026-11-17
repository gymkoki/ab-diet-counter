"""こまめ記録ポイント100ポイントで「キーラン君ステッカープレゼント」を1回だけ出す回帰テスト
（オーナー指示 2026-11-17：「100ポイントたまったら、キーラン君ステッカープレゼントというメッセージを1回表示されるように」）。

・100ポイント以上で、まだ表示していない会員にだけ sticker_due を返す
・表示する直前に /api/meal-points/sticker で「この会員の1回」を確保する（1人1行。2回目は claimed=False）
・100ポイント未満では確保できない（端末の申告だけでは出さない）
・管理画面に達成者の一覧（ジムでステッカーを渡すため）
"""
import datetime
import os

import pytest

import app as m

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "templates", "index.html")
ADMIN_HTML = os.path.join(ROOT, "templates", "admin.html")


@pytest.fixture
def client():
    m.init_db()
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("meal_points", "meal_point_bonus", "meal_point_sticker"):
        cur.execute(f"DELETE FROM {t}")
    cur.execute("DELETE FROM user_profile WHERE user_id LIKE 'stk-%'")
    conn.commit()
    conn.close()
    yield m.app.test_client()


def _seed(uid, n, bonus=0):
    conn = m._get_conn()
    cur = conn.cursor()
    start = datetime.date(2025, 1, 1)
    for i in range(n):
        cur.execute(f"INSERT INTO meal_points (user_id,date,created_at) VALUES ({m.PH},{m.PH},'x')",
                    (uid, (start + datetime.timedelta(days=i)).isoformat()))
    if bonus:
        cur.execute(f"INSERT INTO meal_point_bonus (user_id,reason,points,created_at) VALUES ({m.PH},'onboarding',{m.PH},'x')",
                    (uid, bonus))
    conn.commit()
    conn.close()


def _points(client, uid):
    return client.get(f"/api/meal-points?user_id={uid}").get_json()


def test_goal_is_100():
    assert m.MEAL_POINTS_STICKER_GOAL == 100


def test_not_due_below_100(client):
    _seed("stk-a", 99)
    d = _points(client, "stk-a")
    assert d["points"] == 99 and d["sticker_due"] is False
    r = client.post("/api/meal-points/sticker", json={"user_id": "stk-a"}).get_json()
    assert r["claimed"] is False, "100ポイント未満では確保できない"
    assert _points(client, "stk-a")["sticker_due"] is False


def test_due_at_100_including_bonus(client):
    _seed("stk-b", 99, bonus=1)
    d = _points(client, "stk-b")
    assert d["points"] == 100 and d["sticker_due"] is True and d["sticker_goal"] == 100


def test_shown_only_once(client):
    _seed("stk-c", 120)
    assert _points(client, "stk-c")["sticker_due"] is True
    first = client.post("/api/meal-points/sticker", json={"user_id": "stk-c"}).get_json()
    second = client.post("/api/meal-points/sticker", json={"user_id": "stk-c"}).get_json()
    assert first["claimed"] is True and second["claimed"] is False, "2つ目の端末・2回目は出さない"
    assert _points(client, "stk-c")["sticker_due"] is False


def test_bad_user_id(client):
    assert client.post("/api/meal-points/sticker", json={}).status_code == 400


def test_admin_lists_sticker_members(client):
    _seed("stk-d", 100)
    _seed("stk-e", 30)
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO user_profile (user_id, display_name, updated_at) VALUES ({m.PH},'<b>たなか</b>','x')",
                ("stk-d",))
    conn.commit()
    conn.close()
    d = client.get("/api/admin/special-message", headers=ADMIN).get_json()
    assert d["sticker_goal"] == 100
    assert [x["name"] for x in d["sticker_members"]] == ["<b>たなか</b>"]
    assert d["sticker_members"][0]["shown_on"] is None
    client.post("/api/meal-points/sticker", json={"user_id": "stk-d"})
    d = client.get("/api/admin/special-message", headers=ADMIN).get_json()
    assert d["sticker_members"][0]["shown_on"] == datetime.datetime.now(m.JST).date().isoformat()
    # 名前はエスケープして出す
    src = open(ADMIN_HTML, encoding="utf-8").read()
    body = src[src.index("function stickerList"):src.index("async function saveSpecialMessage")]
    assert "escapeHtml(x.name)" in body


def test_app_shows_message_once():
    src = open(INDEX, encoding="utf-8").read()
    assert 'id="sticker-overlay"' in src
    i = src.index('id="sticker-overlay"')
    block = src[i:i + 1200]
    assert "キーラン君ステッカー" in block and "プレゼント" in block
    body = src[src.index("async function maybeShowSticker"):src.index("function closeSticker")]
    assert "d.sticker_due" in body, "サーバーが sticker_due を返したときだけ"
    assert "/api/meal-points/sticker" in body and "c.claimed" in body, "表示の直前にサーバーで1回を確保する"
    assert "STICKER_LOCAL_KEY" in body and "_stickerBlocked()" in body
    # ポイントを取りに行ったとき・送ったときの両方で確かめる
    assert src.count("maybeShowSticker(d)") >= 2


def test_app_does_not_overlap_other_notices():
    """Codex の指摘（PR #269）：①ホーム画面に追加の案内も待つ ②起動時の確認が時間切れなら見送る
    ③確保したあとも、ほかのお知らせが開いていれば閉じるまで待ってから出す。"""
    src = open(INDEX, encoding="utf-8").read()
    blocked = src[src.index("function _stickerBlocked"):src.index("async function maybeShowSticker")]
    assert "#home-banner" in blocked
    body = src[src.index("async function maybeShowSticker"):src.index("function closeSticker")]
    assert "if (!settled) return;" in body
    after_claim = body[body.index("c.claimed"):]
    assert "while (_stickerBlocked())" in after_claim
    assert after_claim.index("while (_stickerBlocked())") < after_claim.index("classList.add('show')")
    # 確保したが表示できずに閉じた端末は、次に開いたときに出す
    assert "'pending'" in body


def test_other_tab_already_shown_is_rechecked():
    """Codex の指摘（PR #269 2回目）：pending から再開する2つのタブで2回出ないよう、表示の直前に共有の記録を読み直す。"""
    src = open(INDEX, encoding="utf-8").read()
    body = src[src.index("async function maybeShowSticker"):src.index("function closeSticker")]
    tail = body[body.rindex("while (_stickerBlocked())"):]
    recheck = tail.index("localStorage.getItem(STICKER_LOCAL_KEY) !== 'pending') return;")
    assert recheck < tail.index("setItem(STICKER_LOCAL_KEY, 'shown')") < tail.index("classList.add('show')")
