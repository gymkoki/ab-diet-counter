# 個別の声かけメッセージ（AIが下書き → オーナーが承認 → 会員へ通知）の回帰テスト。
# 方針（オーナー確認 2026-08）：承認制。会員に自動でメッセージが飛ぶことは絶対にない。
# 最重要の不変条件：status='draft' の下書きは、会員側APIから絶対に見えないこと。

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
os.environ.setdefault("ADMIN_PASSWORD", "testpw")

import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": "testpw"}


def _reset_db():
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        for t in ("coach_messages", "user_profile", "daily_b_count", "daily_weight"):
            cur.execute(f"DELETE FROM {t}")
        conn.commit()
    finally:
        conn.close()


def _seed(members):
    """members: [(uid, name, last_record_days_ago, weight_change_kg)]"""
    now = datetime.datetime.now(m.JST)
    today = now.date()

    def d(n):
        return (today - datetime.timedelta(days=n)).isoformat()

    conn = m._get_conn()
    try:
        cur = conn.cursor()
        PH = m.PH
        for uid, name, last_days, w_change in members:
            cur.execute(
                f"INSERT INTO user_profile (user_id, display_name, goal, updated_at) "
                f"VALUES ({PH},{PH},{PH},{PH})",
                (uid, name, "cut", now.isoformat()),
            )
            if last_days == 0:
                for n in range(0, 7):
                    cur.execute(
                        f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) "
                        f"VALUES ({PH},{PH},{PH},{PH})",
                        (uid, d(n), 5.0, now.isoformat()),
                    )
            else:
                cur.execute(
                    f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) "
                    f"VALUES ({PH},{PH},{PH},{PH})",
                    (uid, d(last_days), 5.0, now.isoformat()),
                )
            if w_change is not None:
                cur.execute(
                    f"INSERT INTO daily_weight (user_id,date,weight,created_at) "
                    f"VALUES ({PH},{PH},{PH},{PH})",
                    (uid, d(28), 60.0, now.isoformat()),
                )
                cur.execute(
                    f"INSERT INTO daily_weight (user_id,date,weight,created_at) "
                    f"VALUES ({PH},{PH},{PH},{PH})",
                    (uid, d(0), 60.0 + w_change, now.isoformat()),
                )
        conn.commit()
    finally:
        conn.close()


def _stub_ai(messages_by_short):
    class _Block:
        type = "text"

        def __init__(self, t):
            self.text = t

    class _Msg:
        def __init__(self, t):
            self.content = [_Block(t)]

    class _Messages:
        def create(self, **kw):
            return _Msg(json.dumps(
                {"messages": [{"user_id_short": k, "message": v}
                              for k, v in messages_by_short.items()]},
                ensure_ascii=False,
            ))

    class _Client:
        messages = _Messages()

    m.get_client = lambda: _Client()


def test_targets_only_members_needing_outreach():
    """順調な会員は声かけ対象にならないこと。"""
    _reset_db()
    # 記録途絶10日 / 体重+1.5kg / 順調（毎日記録・体重-1.2kg）
    _seed([("u-a", "山田", 10, None), ("u-b", "鈴木", 0, 1.5), ("u-c", "佐藤", 0, -1.2)])
    picked = m._members_needing_outreach(m._collect_cut_member_stats())
    assert sorted(p["uid"] for p in picked) == ["u-a", "u-b"], \
        "順調な会員が声かけ対象に含まれています"
    # 理由が記録されていること
    reasons = {p["uid"]: p["reasons"] for p in picked}
    assert any("途絶" in r for r in reasons["u-a"])
    assert any("増加" in r for r in reasons["u-b"])


def test_draft_is_never_visible_to_member():
    """【最重要】下書きは会員側APIから絶対に見えないこと。"""
    _reset_db()
    _seed([("u-a", "山田", 10, None)])
    _stub_ai({"u-a": "こんにちは、また記録してみませんか？"})
    c = m.app.test_client()

    r = c.post("/api/admin/coach-messages/generate", headers=ADMIN)
    assert r.status_code == 200 and r.get_json()["created"] == 1

    # 会員側からは空
    assert c.get("/api/coach-messages?user_id=u-a").get_json()["items"] == [], \
        "承認前の下書きが会員に見えています（自動送信になっている）"
    # 管理側からは見える
    assert len(c.get("/api/admin/coach-messages", headers=ADMIN).get_json()["drafts"]) == 1


def test_send_delivers_only_to_that_member():
    """送信した会員だけに、編集後の文面が届くこと。"""
    _reset_db()
    _seed([("u-a", "山田", 10, None), ("u-b", "鈴木", 0, 1.5)])
    _stub_ai({"u-a": "Aさんへの文面", "u-b": "Bさんへの文面"})
    c = m.app.test_client()
    c.post("/api/admin/coach-messages/generate", headers=ADMIN)

    drafts = c.get("/api/admin/coach-messages", headers=ADMIN).get_json()["drafts"]
    a = [x for x in drafts if x["user_id"] == "u-a"][0]
    c.post(f"/api/admin/coach-messages/{a['id']}/send",
           json={"message": "編集した文面です"}, headers=ADMIN)

    got_a = c.get("/api/coach-messages?user_id=u-a").get_json()["items"]
    got_b = c.get("/api/coach-messages?user_id=u-b").get_json()["items"]
    assert len(got_a) == 1 and got_a[0]["message"] == "編集した文面です"
    assert got_a[0]["read"] is False
    assert got_b == [], "送信していない会員にメッセージが届いています"


def test_discarded_draft_is_never_delivered():
    """破棄した下書きは会員に届かないこと。"""
    _reset_db()
    _seed([("u-a", "山田", 10, None)])
    _stub_ai({"u-a": "文面"})
    c = m.app.test_client()
    c.post("/api/admin/coach-messages/generate", headers=ADMIN)
    mid = c.get("/api/admin/coach-messages", headers=ADMIN).get_json()["drafts"][0]["id"]
    c.post(f"/api/admin/coach-messages/{mid}/discard", headers=ADMIN)
    assert c.get("/api/coach-messages?user_id=u-a").get_json()["items"] == [], \
        "破棄したメッセージが届いています"


def test_mark_read_and_other_user_cannot_read():
    """既読にできること／他人のメッセージは見えないこと。"""
    _reset_db()
    _seed([("u-a", "山田", 10, None)])
    _stub_ai({"u-a": "文面"})
    c = m.app.test_client()
    c.post("/api/admin/coach-messages/generate", headers=ADMIN)
    mid = c.get("/api/admin/coach-messages", headers=ADMIN).get_json()["drafts"][0]["id"]
    c.post(f"/api/admin/coach-messages/{mid}/send", headers=ADMIN)

    items = c.get("/api/coach-messages?user_id=u-a").get_json()["items"]
    c.post("/api/coach-messages/read", json={"user_id": "u-a", "ids": [items[0]["id"]]})
    assert c.get("/api/coach-messages?user_id=u-a").get_json()["items"][0]["read"] is True
    # 他人には見えない
    assert c.get("/api/coach-messages?user_id=u-other").get_json()["items"] == []


def test_cooldown_prevents_repeat_drafts():
    """同じ会員に短期間で繰り返し下書きを作らないこと（連投防止）。"""
    _reset_db()
    _seed([("u-a", "山田", 10, None)])
    _stub_ai({"u-a": "文面"})
    c = m.app.test_client()
    c.post("/api/admin/coach-messages/generate", headers=ADMIN)
    mid = c.get("/api/admin/coach-messages", headers=ADMIN).get_json()["drafts"][0]["id"]
    c.post(f"/api/admin/coach-messages/{mid}/send", headers=ADMIN)

    # 直後にもう一度作っても、同じ人は対象外になる
    r = c.post("/api/admin/coach-messages/generate", headers=ADMIN)
    assert r.get_json()["created"] == 0, "直近に送った会員へ連続で下書きが作られています"


def test_admin_endpoints_require_auth():
    """未認証では管理APIが404（存在を隠す）であること。"""
    c = m.app.test_client()
    assert c.get("/api/admin/coach-messages").status_code == 404
    assert c.post("/api/admin/coach-messages/generate").status_code == 404
    assert c.post("/api/admin/coach-messages/send-all").status_code == 404


def test_prompt_writes_to_member_not_owner():
    """AIプロンプトが「会員本人が読む文章」を書く指示になっていること。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app.py"), encoding="utf-8").read()
    assert "このメッセージは会員本人が読みます" in src
    assert "オーナー向けの指示や分析ではありません" in src
    # 安全面（極端な食事制限を勧めない・責めない）
    assert "極端な糖質制限・絶食・断食は絶対に提案しない" in src
    assert "責めない・急かさない" in src
