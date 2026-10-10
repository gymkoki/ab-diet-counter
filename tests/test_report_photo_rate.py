# デイリーレポートの各参加者に「直近の食事写真を1日平均何枚上げているか」を出す回帰テスト
# （オーナー指示 2026-10-10）。
#
# 数え方（「記録のしかたの違い」の photos_per_day と同じ）：
#   ・写真の枚数＝action_log の「写真で記録」（解析が終わった写真1枚ごとに1件）
#   ・割る日数＝直近7日のうち、写真・文章・コピー・B手入力のどれかで記録した日
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
        return m._member_photo_rate(conn.cursor(), uid)
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


def test_photos_per_recorded_day(db):
    _log("r1", 1, 8, "photo"); _log("r1", 1, 12, "photo"); _log("r1", 1, 19, "photo")
    _log("r1", 1, 15, "text")                      # 文章の記録は写真に数えない
    _log("r1", 3, 12, "photo")
    _log("r1", 4, 19, "text")                      # 写真0枚でも記録した日として数える
    r = _rate("r1")
    assert r["record_days"] == 3 and r["photos"] == 4
    assert r["photos_per_day"] == pytest.approx(1.3)


def test_days_without_any_record_are_not_counted_as_zero(db):
    _log("r2", 2, 12, "photo", n=3)
    assert _rate("r2")["photos_per_day"] == 3.0


def test_only_the_last_seven_days(db):
    _log("r3", 6, 12, "photo")                     # 7日目＝含む
    _log("r3", 7, 12, "photo", n=5)                # 8日前＝含まない
    r = _rate("r3")
    assert r["photos"] == 1 and r["record_days"] == 1


def test_late_night_belongs_to_previous_day(db):
    _log("r4", 1, 20, "photo")
    _log("r4", 0, 2, "photo")                      # 今日の深夜2時＝昨日の晩
    r = _rate("r4")
    assert r["record_days"] == 1 and r["photos"] == 2


def test_no_records_gives_none(db):
    r = _rate("nobody")
    assert r["photos_per_day"] is None and r["record_days"] == 0


def test_report_photos_api_includes_rate(db):
    _seed_member("pr_good", "しゃしん子", 70.0, 67.5)
    _log("pr_good", 1, 8, "photo"); _log("pr_good", 1, 12, "photo")
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        d = c.get("/api/admin/report-photos", headers=ADMIN).get_json()
    me = next(x for x in d["good"] if x["name"] == "しゃしん子")
    assert me["photo_rate"]["photos_per_day"] == 2.0
    assert "pr_good" not in str(me["photo_rate"]), "会員IDをレポートに出さない"


def test_report_card_shows_rate():
    pytest.importorskip("requests", reason="requests 未インストール")
    import send_report as sr
    line = sr._photo_rate_line({"days": 7, "record_days": 5, "photos": 12, "photos_per_day": 2.4})
    assert "1日平均 2.4枚" in line and "記録した5日" in line and "計12枚" in line
    assert "記録なし" in sr._photo_rate_line({"days": 7, "record_days": 0, "photos": 0,
                                               "photos_per_day": None})
    assert sr._photo_rate_line(None) == "", "古いサーバー（photo_rate なし）でも落ちない"

    member = {"name": "しゃしん子", "change_30d_kg": -2.5, "avg_b_7d": 3.1, "b_target": 4,
              "photos": [{"src": PHOTO, "date": "2026-10-09", "b_count": 2}],
              "photo_rate": {"days": 7, "record_days": 5, "photos": 12, "photos_per_day": 2.4}}
    html = sr._photo_card(member, "t", {}, True)
    assert "📷 食事の写真：" in html and "1日平均 2.4枚" in html
    assert "掲載 1枚" in html, "下に並べた写真の枚数と、1日平均の枚数を取り違えないこと"
