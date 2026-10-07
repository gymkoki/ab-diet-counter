# 「お名前の登録のお願い」の回帰テスト（オーナー指示 2026-10-07）。
#
# 経緯：お名前が未設定の会員は管理画面で「会員a463」のように表示され、誰の記録か分からない。
#   お名前が未設定の会員へ、コーチメッセージとして登録のお願いを届ける。
#
# 守りたい不変条件：
#   ①お名前が未設定で、前日までに記録がある会員にだけ届く
#   ②お名前がある会員・入会したその日の会員には届かない
#   ③1人1回だけ（読んだあとも二度と届かない）
#   ④アプリ側は「お名前を登録する」ボタンでお名前欄へ進める
#   ⑤端末にだけあるお名前は、メッセージ確認より先にサーバーへ送る（誤って届かないように）

import datetime
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app as m  # noqa: E402

PREFIX = "name-req-"


def _exec(sql, params=()):
    with m._db_lock:
        conn = m._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


@pytest.fixture
def db():
    m.init_db()

    def clean():
        for t in ("coach_messages", "user_profile", "daily_b_count"):
            _exec(f"DELETE FROM {t} WHERE user_id LIKE {m.PH}", (PREFIX + "%",))
    clean()
    yield
    clean()


def _record(uid, days_ago):
    d = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=days_ago)).isoformat()
    _exec(f"INSERT INTO daily_b_count (user_id, date, b_count, created_at) "
          f"VALUES ({m.PH},{m.PH},3,{m.PH})", (uid, d, d))


def _name(uid, name):
    _exec(f"INSERT INTO user_profile (user_id, display_name, updated_at) VALUES ({m.PH},{m.PH},'x')",
          (uid, name))


def _get(uid):
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        r = c.get(f"/api/coach-messages?user_id={uid}")
    assert r.status_code == 200
    return r.get_json()["items"]


def test_nameless_member_gets_request(db):
    uid = PREFIX + "a463"
    _record(uid, 3)
    items = _get(uid)
    assert len(items) == 1
    assert items[0]["ask_name"] is True and not items[0]["read"]
    assert "ニックネーム" in items[0]["message"] and "苗字" in items[0]["message"]


def test_blank_name_counts_as_nameless(db):
    uid = PREFIX + "blank"
    _name(uid, "   ")
    _record(uid, 1)
    assert [i["ask_name"] for i in _get(uid)] == [True]


def test_named_member_gets_nothing(db):
    uid = PREFIX + "named"
    _name(uid, "奥松")
    _record(uid, 3)
    assert _get(uid) == []


def test_brand_new_member_gets_nothing(db):
    """入会したその日（今日の記録しかない）は、最初の設定の途中かもしれないので送らない。"""
    uid = PREFIX + "new"
    _record(uid, 0)
    assert _get(uid) == []
    assert _get(PREFIX + "never-recorded") == []


def test_sent_only_once(db):
    uid = PREFIX + "once"
    _record(uid, 2)
    first = _get(uid)
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        c.post("/api/coach-messages/read", json={"user_id": uid, "ids": [first[0]["id"]]})
    again = _get(uid)
    assert len(again) == 1 and again[0]["read"], "読んだあとに、もう一度お願いが届いている"


def test_normal_messages_are_not_name_requests(db):
    uid = PREFIX + "coach"
    _name(uid, "山田")
    _exec(f"INSERT INTO coach_messages (user_id, name, reason, message, status, created_at, sent_at) "
          f"VALUES ({m.PH},'山田','手動作成（指名して送信）','がんばってます！','sent','t','t')", (uid,))
    items = _get(uid)
    assert len(items) == 1 and items[0]["ask_name"] is False


def test_db_failure_does_not_break_messages(db, monkeypatch):
    def boom(cur, uid):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "_maybe_queue_name_request", boom)
    assert _get(PREFIX + "x") == []


# ── アプリ側 ──────────────────────────────────────────────
def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def test_app_has_register_name_button():
    html = _html()
    assert 'id="coach-msg-name-btn"' in html and "お名前を登録する" in html
    assert "function coachMsgEditName()" in html
    body = re.search(r"function coachMsgEditName\(\) \{(.*?)\n\}", html, re.S).group(1)
    assert "input-name" in body and "focus()" in body


def test_app_syncs_local_name_before_checking_messages():
    html = _html()
    assert "function _syncNameToServer()" in html
    assert re.search(r"_syncNameToServer\(\)\s*//[^\n]*\n\s*\.then\(checkCoachMessages\)", html), \
        "端末にだけあるお名前を送る前にメッセージを確認している"


def test_name_request_does_not_block_coach_drafts(db):
    """お願いを送った人にも、応援の声かけの下書きは通常どおり作れること。"""
    uid = PREFIX + "cool"
    _record(uid, 2)
    _get(uid)
    assert uid not in m._recent_dm_user_ids()
