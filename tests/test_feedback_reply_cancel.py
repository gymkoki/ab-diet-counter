"""修正希望・ご要望への返信の「送信取り消し」の回帰テスト（オーナー指示 2026-10-25）。

管理画面の受信箱で送った返信を取り消せるようにした。取り消すと
  ・本人のアプリ（/api/feedback-replies）から返信が消える
  ・受信箱は「未返信」に戻り、書き直して送り直せる
取り消しは {"cancel": true} を明示したときだけ。空の本文を送っても、送った返信は消さない。
"""
import datetime
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
UID = "u-fb-cancel"


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def fid():
    m.init_db()
    ts = datetime.datetime.now(m.JST).isoformat()
    with m._db_lock:
        conn = m._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM feedback WHERE user_id={m.PH}", (UID,))
            cur.execute(f"INSERT INTO feedback (user_id, name, text, created_at) VALUES ({m.PH},{m.PH},{m.PH},{m.PH})",
                        (UID, "テスト", "バナナのBがぶれます", ts))
            conn.commit()
            cur.execute(f"SELECT id FROM feedback WHERE user_id={m.PH}", (UID,))
            return cur.fetchone()[0]
        finally:
            conn.close()


def _member_replies(c):
    return c.get(f"/api/feedback-replies?user_id={UID}").get_json()["items"]


def _admin_item(c, fid):
    items = c.get("/api/admin/feedback", headers=ADMIN).get_json()["items"]
    return next(i for i in items if i["id"] == fid)


def test_cancel_removes_reply_from_member_app(fid):
    c = m.app.test_client()
    assert c.post(f"/api/admin/feedback/{fid}/reply", json={"text": "直しました"}, headers=ADMIN).status_code == 200
    assert [i["reply_text"] for i in _member_replies(c)] == ["直しました"]

    r = c.post(f"/api/admin/feedback/{fid}/reply", json={"cancel": True}, headers=ADMIN)
    assert r.status_code == 200
    assert _member_replies(c) == []
    item = _admin_item(c, fid)
    assert not item["reply_text"] and not item["replied_at"]


def test_cancel_after_member_read_also_hides_it(fid):
    c = m.app.test_client()
    c.post(f"/api/admin/feedback/{fid}/reply", json={"text": "直しました"}, headers=ADMIN)
    c.post("/api/feedback-replies/read", json={"user_id": UID, "ids": [fid]})
    assert _admin_item(c, fid)["read_at"]
    c.post(f"/api/admin/feedback/{fid}/reply", json={"cancel": True}, headers=ADMIN)
    assert _member_replies(c) == []
    assert not _admin_item(c, fid)["read_at"]


def test_can_resend_after_cancel(fid):
    c = m.app.test_client()
    c.post(f"/api/admin/feedback/{fid}/reply", json={"text": "まちがい"}, headers=ADMIN)
    c.post(f"/api/admin/feedback/{fid}/reply", json={"cancel": True}, headers=ADMIN)
    c.post(f"/api/admin/feedback/{fid}/reply", json={"text": "正しい返信"}, headers=ADMIN)
    items = _member_replies(c)
    assert [i["reply_text"] for i in items] == ["正しい返信"] and items[0]["read"] is False


def test_empty_text_does_not_erase_sent_reply(fid):
    c = m.app.test_client()
    c.post(f"/api/admin/feedback/{fid}/reply", json={"text": "直しました"}, headers=ADMIN)
    r = c.post(f"/api/admin/feedback/{fid}/reply", json={"text": "  "}, headers=ADMIN)
    assert r.status_code == 400
    assert [i["reply_text"] for i in _member_replies(c)] == ["直しました"]


def test_cancel_is_admin_only(fid):
    c = m.app.test_client()
    c.post(f"/api/admin/feedback/{fid}/reply", json={"text": "直しました"}, headers=ADMIN)
    assert c.post(f"/api/admin/feedback/{fid}/reply", json={"cancel": True}).status_code == 404
    assert len(_member_replies(c)) == 1


def test_admin_inbox_has_cancel_button():
    html = _read("templates/admin.html")
    assert "送信を取り消す" in html
    m_ = re.search(r"async function cancelFeedbackReply\(id, alreadyRead\)\s*\{.*?\n  \}", html, re.S)
    assert m_, "cancelFeedbackReply が見つかりません"
    body = m_.group(0)
    assert "cancel: true" in body and "confirm(" in body
    assert "すでに相手が読んでいます" in body   # 既読の返信は「読まれた内容は戻せない」と伝える
