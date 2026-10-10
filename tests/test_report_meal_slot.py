"""デイリーレポートの食事写真に「朝」「昼」「夕」「間食」を添える回帰テスト。

オーナー指示 2026-10-10：
  デイリーレポートの写真は、その食事が朝・昼・夕・間食のどれにあたるかを
  ざっくり推測して「朝」「昼」「夕」「間食」と書く。

推測のしかた（app.py の _guess_meal_slot）：
  ・記録の id は端末で作った時刻（Date.now()×100＋連番）→ 記録した時刻の時間帯で決める
    （朝 4:00〜10:30／昼 10:30〜15:00／間食 15:00〜17:00／夕 17:00〜翌4:00）
  ・お菓子・スイーツ・ジュースだけの記録や、ごく少量（120kcal未満）の記録は時間帯にかかわらず「間食」
  ・別の日に後から入れた記録は時刻があてにならないので付けない（間食らしい中身なら「間食」）
"""
import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
import app as m  # noqa: E402
from test_report_photos import PHOTO  # noqa: E402

DAY = "2026-10-07"
MEAL = {"foods": [{"name": "白米", "kcal_per_serving": 250},
                  {"name": "焼き魚", "kcal_per_serving": 180}], "total_b_count": 1}


def _id_at(date, hh, mm, seq=3):
    """端末の nextItemId() と同じ形の id（その時刻に記録した写真）。"""
    d = datetime.date.fromisoformat(date)
    at = datetime.datetime(d.year, d.month, d.day, hh, mm, tzinfo=m.JST)
    return int(at.timestamp() * 1000) * 100 + seq


def _slot(hh, mm, res=MEAL, date=DAY, record_date=DAY):
    return m._guess_meal_slot({"id": _id_at(date, hh, mm)}, res, record_date)


@pytest.mark.parametrize("hh,mm,expected", [
    (4, 0, "朝"), (7, 30, "朝"), (10, 29, "朝"),
    (10, 30, "昼"), (12, 15, "昼"), (14, 59, "昼"),
    (15, 0, "間食"), (16, 30, "間食"),
    (17, 0, "夕"), (19, 45, "夕"), (23, 50, "夕"),
])
def test_slot_follows_recorded_time(hh, mm, expected):
    assert _slot(hh, mm) == expected


def test_late_night_counts_as_previous_dinner():
    """深夜0〜4時の記録は前日の夕食（日付は前日の記録として保存されている）。"""
    assert _slot(1, 10, date="2026-10-08", record_date=DAY) == "夕"


def test_sweets_or_tiny_record_is_snack_any_time():
    sweets = {"foods": [{"name": "チョコ", "sweet_kcal": 150, "kcal_per_serving": 150},
                        {"name": "オレンジジュース", "sweet_kcal": 90, "kcal_per_serving": 90}]}
    tiny = {"foods": [{"name": "飴", "kcal_per_serving": 20}, {"name": "ブラックコーヒー", "kcal_per_serving": 5}]}
    assert _slot(12, 0, res=sweets) == "間食"
    assert _slot(20, 0, res=tiny) == "間食"
    # カロリーが分からない料理を少量扱いにしない
    assert _slot(12, 0, res={"foods": [{"name": "定食"}]}) == "昼"


def test_record_added_on_another_day_gets_no_label():
    """過去日の編集で後から入れた記録は、時刻が食べた時間と関係ないので推測しない。"""
    assert _slot(9, 0, date="2026-10-09", record_date=DAY) is None
    assert m._guess_meal_slot({"id": "cm-abc"}, MEAL, DAY) is None
    assert m._guess_meal_slot({}, MEAL, DAY) is None


def test_legacy_sections_keep_their_slot():
    assert m._guess_meal_slot({}, MEAL, DAY, "breakfast") == "朝"
    assert m._guess_meal_slot({}, MEAL, DAY, "dinner") == "夕"
    assert m._guess_meal_slot({}, MEAL, DAY, "snack") == "間食"


def test_report_photos_api_returns_meal_slot():
    m.init_db()
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_meals")
    payload = {"food": {"items": [
        {"id": _id_at(DAY, 7, 40), "previewSrc": PHOTO, "result": MEAL},
        {"id": _id_at(DAY, 19, 5), "previewSrc": PHOTO, "result": MEAL},
    ]}}
    cur.execute(f"INSERT INTO daily_meals (user_id,date,payload,created_at) VALUES ({m.PH},{m.PH},{m.PH},'x')",
                ("slot-user", DAY, json.dumps(payload)))
    conn.commit()
    try:
        photos = m._member_report_photos(cur, "slot-user", 0, "2026-10-01")
    finally:
        cur.execute("DELETE FROM daily_meals")
        conn.commit()
        conn.close()
    assert [p["meal_slot"] for p in photos] == ["朝", "夕"]


def test_report_card_shows_meal_slot():
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report as sr
    member = {"name": "テスト", "photos": [
        {"date": DAY, "src": PHOTO, "b_count": 1, "day_b_count": 3, "meal_slot": "夕"},
        {"date": DAY, "src": PHOTO, "b_count": 0, "day_b_count": 3, "meal_slot": "間食"},
        {"date": DAY, "src": PHOTO, "b_count": 1, "day_b_count": 3, "meal_slot": None},
    ]}
    html = sr._photo_card(member, "t", {}, True)
    assert f'<b class="ps">夕</b> {DAY}<br>この日の合計 B3' in html
    assert '<b class="ps">間食</b>' in html
    assert html.count('class="ps"') == 2, "推測できない写真にはラベルを付けない"
