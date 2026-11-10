"""アプリ内アンケート（1人1回・3択＋自由記入1行・スキップ可。オーナー指示 2026-11-10）の回帰テスト。

「いちばん手放せない機能は？」。通知の連投にならないよう、表示は1人1回・閉じたら再表示しない。
回答は管理画面の「修正希望・ご要望」の上に集計して出す。
"""
import datetime
import os
import re

import pytest

import app as m

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clean():
    m.init_db()
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("survey_responses", "daily_b_count", "user_profile"):
        cur.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()


@pytest.fixture
def c():
    _clean()
    yield m.app.test_client()
    _clean()


def _member(uid, days_ago=1, name=None):
    d = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=days_ago)).isoformat()
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) VALUES ({m.PH},{m.PH},3,'x')", (uid, d))
    if name:
        cur.execute(f"INSERT INTO user_profile (user_id,display_name,updated_at) VALUES ({m.PH},{m.PH},'x')", (uid, name))
    conn.commit()
    conn.close()


def _show(c, uid):
    return c.get(f"/api/survey?user_id={uid}").get_json()


def test_three_choices_with_the_owners_wording(c):
    _member("u1")
    d = _show(c, "u1")
    assert d["show"] is True
    labels = [o["label"] for o in d["survey"]["options"]]
    assert len(labels) == 3
    assert any("AI解析" in l for l in labels) and any("体重とグラフ" in l for l in labels) \
        and any("こまめ記録ポイント" in l for l in labels)
    assert d["survey"]["question"] == "いちばん手放せない機能は？"


def test_shown_once_even_if_closed_without_answer(c):
    """表示した時点で記録し、閉じても（答えなくても）二度と出さない。"""
    _member("u1")
    c.post("/api/survey", json={"user_id": "u1", "action": "shown"})
    assert _show(c, "u1") == {"show": False, "done": True}
    c.post("/api/survey", json={"user_id": "u1", "action": "skip"})
    assert _show(c, "u1")["show"] is False


def test_not_shown_on_the_first_day(c):
    """入会したその日の人には出さない（前日までに記録がある人だけ）。翌日以降は出る。"""
    _member("new", days_ago=0)
    d = _show(c, "new")
    assert d["show"] is False and not d.get("done"), "「まだ」なので、端末側で終わりにしない"


def test_answer_is_saved_and_not_overwritten(c):
    _member("u1")
    c.post("/api/survey", json={"user_id": "u1", "action": "shown"})
    r = c.post("/api/survey", json={"user_id": "u1", "action": "answer", "choice": "points",
                                    "comment": "  ポイントが\n楽しい " + "あ" * 200})
    assert r.get_json()["ok"] is True
    c.post("/api/survey", json={"user_id": "u1", "action": "skip"})      # 二重送信・遅れて届いたスキップ
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, choice, comment FROM survey_responses WHERE user_id='u1'")
    status, choice, comment = cur.fetchone()
    conn.close()
    assert status == "answered" and choice == "points", "回答済みはスキップで上書きしない"
    assert len(comment) <= m.SURVEY_COMMENT_MAX and "\n" not in comment


def test_rejects_unknown_choice(c):
    r = c.post("/api/survey", json={"user_id": "u1", "action": "answer", "choice": "hack"})
    assert r.status_code == 400


def test_admin_summary(c):
    for uid, ch, cm, nm in (("a", "ai", "写真が便利", "佐藤"), ("b", "ai", "", None), ("d", "weight", "<b>x</b>", "鈴木")):
        _member(uid, name=nm)
        c.post("/api/survey", json={"user_id": uid, "action": "shown"})
        c.post("/api/survey", json={"user_id": uid, "action": "answer", "choice": ch, "comment": cm})
    _member("e")
    c.post("/api/survey", json={"user_id": "e", "action": "shown"})
    c.post("/api/survey", json={"user_id": "e", "action": "skip"})
    assert c.get("/api/admin/survey-results").status_code in (401, 403, 404)
    d = c.get("/api/admin/survey-results", headers=ADMIN).get_json()
    assert d["answered"] == 3 and d["skipped"] == 1 and d["shown"] == 4
    res = {x["key"]: x for x in d["results"]}
    assert res["ai"]["count"] == 2 and res["ai"]["pct"] == 67 and res["weight"]["count"] == 1
    assert res["points"]["count"] == 0
    assert {x["comment"] for x in d["comments"]} == {"写真が便利", "<b>x</b>"}, "空のひとことは出さない"
    assert any(x["name"] == "佐藤" for x in d["comments"])


# ── アプリ側：1回だけ・ほかのお知らせと重ねない ──
def _html():
    return open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()


def test_app_marks_shown_before_user_acts():
    body = re.search(r"function showSurvey\(sv\) \{.*?\n\}", _html(), re.S).group(0)
    assert "localStorage.setItem(SURVEY_LOCAL_KEY" in body and "action: 'shown'" in body


def test_app_waits_for_other_popups():
    body = re.search(r"async function maybeShowSurvey\(\) \{.*?\n\}", _html(), re.S).group(0)
    assert body.count("_anyOverlayOpen()") >= 2, "表示の直前にも、ほかのお知らせが出ていないか確かめる"
    assert "localStorage.getItem(SURVEY_LOCAL_KEY)" in body


def test_app_has_skip_and_one_line_comment():
    html = _html()
    assert "skipSurvey()" in html and 'id="survey-comment" type="text" maxlength="100"' in html


def test_admin_shows_results_in_feedback_card():
    admin = open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8").read()
    assert 'id="survey-summary"' in admin and "loadSurveyResults()" in admin
    assert admin.index('id="survey-summary"') < admin.index('id="fb-list"')
