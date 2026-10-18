# デイリーレポートの各参加者に「直近7日のタンパク質・野菜の1日平均」を出す回帰テスト
# （オーナー指示 2026-10-15「この箇所に野菜とタンパク質の摂取量も併せて書いて」）。
#
# 数え方：
#   ・元データは daily_nutrition（明細が5日で消えても残る1日ぶんのサマリー）
#   ・直近7日・昨日まで（レポートは朝8時に作るので、今日は朝食くらいしか入っていない）
#   ・記録が2回未満の日は平均に入れない（「減量達成者の食事内容」と同じ ACHIEVER_MIN_MEALS）
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
import app as m  # noqa: E402
from test_report_photos import ADMIN, PHOTO, _clear_rotation, _reset_db, _seed_member  # noqa: E402


def _clear():
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_nutrition")
    conn.commit()
    conn.close()
    _clear_rotation()


def _day(uid, days_ago, meals, protein, veg):
    d = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=days_ago)).isoformat()
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO daily_nutrition (user_id, date, meal_count, protein_g, veg_g, created_at) "
                f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH},{m.PH},'x')", (uid, d, meals, protein, veg))
    conn.commit()
    conn.close()


def _intake(uid):
    conn = m._get_conn()
    try:
        return m._member_intake(conn.cursor(), uid)
    finally:
        conn.close()


@pytest.fixture
def db():
    m.init_db()
    _reset_db()
    _clear()
    yield
    _reset_db()
    _clear()


def test_average_of_recent_days(db):
    _day("i1", 1, 3, 70, 200)
    _day("i1", 2, 4, 50, 100)
    r = _intake("i1")
    assert r["protein_g"] == 60 and r["veg_g"] == 150 and r["used_days"] == 2


def test_days_with_a_single_record_are_excluded(db):
    _day("i2", 1, 3, 80, 250)
    _day("i2", 2, 1, 10, 0)            # 1回だけの日＝記録できた量でしかないので外す
    r = _intake("i2")
    assert r["protein_g"] == 80 and r["veg_g"] == 250
    assert r["used_days"] == 1 and r["record_days"] == 2


def test_today_and_older_than_seven_days_are_excluded(db):
    _day("i3", 0, 1, 5, 0)             # 今日（朝食だけ）＝外す
    _day("i3", 0 + 8, 3, 200, 900)     # 8日前＝外す
    _day("i3", 7, 3, 60, 120)          # 7日前＝含む（昨日までの7日間）
    r = _intake("i3")
    assert r["protein_g"] == 60 and r["veg_g"] == 120 and r["used_days"] == 1


def test_no_usable_days_gives_none_values(db):
    r = _intake("nobody")
    assert r["protein_g"] is None and r["veg_g"] is None and r["used_days"] == 0


def test_report_photos_api_includes_intake(db):
    _seed_member("in_good", "やさい子", 70.0, 67.5)
    _day("in_good", 1, 3, 75, 320)
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        d = c.get("/api/admin/report-photos", headers=ADMIN).get_json()
    me = next(x for x in d["good"] if x["name"] == "やさい子")
    assert me["intake"]["protein_g"] == 75 and me["intake"]["veg_g"] == 320
    assert "in_good" not in str(me["intake"]), "会員IDをレポートに出さない"


def test_report_card_shows_intake():
    pytest.importorskip("requests", reason="requests 未インストール")
    import send_report as sr
    intake = {"days": 7, "record_days": 6, "used_days": 5, "min_meals": 2, "protein_g": 68, "veg_g": 240}
    line = sr._intake_line(intake)
    assert "タンパク質 <b>68g</b>" in line and "野菜 <b>240g</b>" in line
    assert "記録が2回以上の5日" in line
    assert "ありません" in sr._intake_line({"days": 7, "used_days": 0, "min_meals": 2,
                                           "protein_g": None, "veg_g": None})
    assert sr._intake_line(None) == "", "古いサーバー（intake なし）でも落ちない"

    member = {"name": "やさい子", "change_30d_kg": -2.5, "photos": [{"src": PHOTO, "date": "2026-10-14"}],
              "record_rate": {"days": 7, "record_days": 5, "records": 18, "records_per_day": 3.6},
              "intake": intake}
    html = sr._photo_card(member, "t", {}, True)
    # 「📝 食事の記録」の行の下に出す（2026-10-18 に「📷 食事の写真」から言い換えた）
    assert html.index("📝 食事の記録：") < html.index("🥗 食べた量：")
