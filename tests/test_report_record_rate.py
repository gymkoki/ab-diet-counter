# デイリーレポートの各参加者に「直近7日に食事を1日平均何回記録しているか」を出す回帰テスト。
# 2026-10-10 は「写真の枚数」だったが、オーナー指示（2026-10-18）で「食事記録の回数」に変えた：
#   「大事なのはこまめに報告すること。間食・コピーご飯・文章での入力も含めて、
#    写真の枚数ではなく食事記録の回数（1日平均何回）にして」
#
# 数え方：
#   ・記録の回数＝action_log の記録操作（写真・文章・コピーご飯・B手入力）。間食もそれぞれ1回
#   ・割る日数＝直近7日のうち、どれかで記録した日（記録なしの日を0回として数えない）
#   ・深夜0〜3時の記録は前日の晩
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
import app as m  # noqa: E402
from test_report_photos import ADMIN, PHOTO, _clear_rotation, _reset_db, _seed_member  # noqa: E402


def _clear_actions():
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM action_log")
    conn.commit()
    conn.close()
    _clear_rotation()


def _log(uid, days_ago, hour, action, n=1):
    today = datetime.datetime.now(m.JST).date()
    ts = datetime.datetime.combine(today - datetime.timedelta(days=days_ago),
                                   datetime.time(hour, 0), tzinfo=m.JST).isoformat()
    conn = m._get_conn()
    cur = conn.cursor()
    for _ in range(n):
        cur.execute(f"INSERT INTO action_log (user_id, action, created_at) VALUES ({m.PH},{m.PH},{m.PH})",
                    (uid, action, ts))
    conn.commit()
    conn.close()


def _rate(uid):
    conn = m._get_conn()
    try:
        return m._member_record_rate(conn.cursor(), uid)
    finally:
        conn.close()


@pytest.fixture
def db():
    m.init_db()
    _reset_db()
    _clear_actions()
    yield
    _reset_db()
    _clear_actions()


def test_counts_every_kind_of_record(db):
    """写真だけでなく、文章・コピーご飯・間食の記録もすべて回数に数える。"""
    _log("r1", 1, 8, "photo"); _log("r1", 1, 12, "photo"); _log("r1", 1, 19, "copy")
    _log("r1", 1, 15, "text")                      # 間食を文章で記録
    _log("r1", 3, 12, "photo")
    _log("r1", 4, 19, "text")                      # 写真0枚の日も記録した日
    r = _rate("r1")
    assert r["record_days"] == 3 and r["records"] == 6
    assert r["records_per_day"] == pytest.approx(2.0)
    assert r["by_kind"] == {"photo": 3, "text": 2, "copy": 1, "manual_b": 0}


def test_text_only_member_is_not_zero(db):
    """写真を撮らず文章だけで記録している人も「こまめに記録している」と分かる（写真0枚にならない）。"""
    for h in (8, 12, 15, 19):
        _log("r5", 1, h, "text")
    assert _rate("r5")["records_per_day"] == 4.0


def test_days_without_any_record_are_not_counted_as_zero(db):
    _log("r2", 2, 12, "photo", n=3)
    assert _rate("r2")["records_per_day"] == 3.0


def test_only_the_last_seven_days(db):
    _log("r3", 6, 12, "copy")                      # 7日目＝含む
    _log("r3", 7, 12, "photo", n=5)                # 8日前＝含まない
    r = _rate("r3")
    assert r["records"] == 1 and r["record_days"] == 1


def test_late_night_belongs_to_previous_day(db):
    _log("r4", 1, 20, "photo")
    _log("r4", 0, 2, "text")                       # 今日の深夜2時＝昨日の晩
    r = _rate("r4")
    assert r["record_days"] == 1 and r["records"] == 2


def test_no_records_gives_none(db):
    r = _rate("nobody")
    assert r["records_per_day"] is None and r["record_days"] == 0


def test_report_photos_api_includes_rate(db):
    _seed_member("pr_good", "きろく子", 70.0, 67.5)
    _log("pr_good", 1, 8, "photo"); _log("pr_good", 1, 12, "copy"); _log("pr_good", 1, 15, "text")
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        d = c.get("/api/admin/report-photos", headers=ADMIN).get_json()
    me = next(x for x in d["good"] if x["name"] == "きろく子")
    assert me["record_rate"]["records_per_day"] == 3.0
    assert "photo_rate" not in me, "写真の枚数ではなく記録の回数を返す"
    assert "pr_good" not in str(me["record_rate"]), "会員IDをレポートに出さない"


def test_report_card_shows_rate():
    pytest.importorskip("requests", reason="requests 未インストール")
    import send_report as sr
    rate = {"days": 7, "record_days": 5, "records": 18, "records_per_day": 3.6,
            "by_kind": {"photo": 12, "text": 2, "copy": 4, "manual_b": 0}}
    line = sr._record_rate_line(rate)
    assert "📝 食事の記録：" in line and "1日平均 3.6回" in line
    assert "記録した5日" in line and "計18回" in line
    assert "写真12・文章2・コピーご飯4" in line and "B手入力" not in line
    assert "枚" not in line, "写真の枚数の表示に戻さない"
    assert "記録なし" in sr._record_rate_line({"days": 7, "record_days": 0, "records": 0,
                                                "records_per_day": None})
    assert sr._record_rate_line(None) == "", "データが無くても落ちない"

    member = {"name": "きろく子", "change_30d_kg": -2.5, "avg_b_7d": 3.1, "b_target": 4,
              "photos": [{"src": PHOTO, "date": "2026-10-09", "b_count": 2}],
              "record_rate": rate}
    html = sr._photo_card(member, "t", {}, True)
    assert "📝 食事の記録：" in html and "1日平均 3.6回" in html
    assert "📷 食事の写真：" not in html
    assert "掲載 1枚" in html
