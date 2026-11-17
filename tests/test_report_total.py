# デイリーレポートの会員カードを「開始時からの通算」で出す回帰テスト（オーナー指示 2026-11-17）。
#
# 「デイリーレポートは、1週間の結果ではなく通算の結果で、減量希望者で痩せていないときは、
#   ランダムにその人の情報が出るように表示してください」。
#   ①「順調／進んでいない」は開始時からの通算の体重変化で分ける（以前は直近30日）
#   ②進んでいない会員は毎日ランダムに選ぶ（同じ日に作り直しても顔ぶれは変わらない）
#   ③カードの数字（食事の記録・運動・タンパク質と野菜・平均B）は開始時からの通算
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
import app as m  # noqa: E402
from test_report_photos import ADMIN, _clear_rotation, _reset_db, _seed_member  # noqa: E402


@pytest.fixture
def db():
    m.init_db()
    _reset_db()
    _clear_rotation()
    m.app.config["TESTING"] = True
    yield
    _reset_db()
    _clear_rotation()


def _weight(uid, days_ago, w):
    d = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=days_ago)).isoformat()
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO daily_weight (user_id,date,weight,created_at) VALUES ({m.PH},{m.PH},{m.PH},'x')",
                    (uid, d, w))
        conn.commit()
    finally:
        conn.close()


def _photos():
    with m.app.test_client() as c:
        return c.get("/api/admin/report-photos", headers=ADMIN).get_json()


# ── ① 通算で分ける ──────────────────────────────────────────────
def test_groups_by_total_change_not_30_days(db):
    # 30日では横ばい（+0.2）だが、開始時からは −3.0kg → 順調
    _seed_member("t-long", "ながく", 60.0, 60.2)
    _weight("t-long", 80, 63.2)
    # 30日では −1.0kg だが、開始時からは +0.5kg → 進んでいない
    _seed_member("t-up", "ふえた", 61.0, 60.0)
    _weight("t-up", 90, 59.5)
    d = _photos()
    good = {x["name"]: x for x in d["good"]}
    bad = {x["name"]: x for x in d["bad"]}
    assert "ながく" in good and good["ながく"]["total_change_kg"] == -3.0
    assert "ふえた" in bad and bad["ふえた"]["total_change_kg"] == 0.5


# ── ② 進んでいない人は毎日ランダム ─────────────────────────────────
def _pool(n):
    return [{"uid": f"u{i}"} for i in range(n)]


def test_bad_group_is_random_by_day_and_stable_within_a_day():
    a = [x["uid"] for x in m._random_order(_pool(12), "2026-11-17", "bad")]
    again = [x["uid"] for x in m._random_order(_pool(12), "2026-11-17", "bad")]
    assert a == again, "同じ日に作り直しても顔ぶれは同じ"
    days = {tuple(x["uid"] for x in m._random_order(_pool(12), f"2026-11-{d:02d}", "bad"))[:3]
            for d in range(1, 15)}
    assert len(days) > 5, "日によって選ばれる人が変わる"
    pinned = [x["uid"] for x in m._random_order(_pool(12), "2026-11-17", "bad", ["u7", "u3"])]
    assert pinned[:2] == ["u7", "u3"], "今日すでに選んだ人を先頭に固定する"


def test_bad_group_uses_random_order_and_good_group_keeps_rotation():
    src = open(os.path.join(os.path.dirname(m.__file__), "app.py"), encoding="utf-8").read()
    body = src[src.index("def admin_report_photos"):src.index("def admin_attention_flags")]
    assert '_random_order(bad_pool, today, "bad"' in body
    assert '_rotation_order(good_pool, history.get("good")' in body


# ── ③ カードの数字は通算 ─────────────────────────────────────────
def test_card_stats_cover_the_whole_period(db):
    _seed_member("t-stat", "つうさん", 60.0, 60.5)
    _weight("t-stat", 50, 60.0)
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        for days_ago, b in ((50, 6.0), (40, 6.0)):
            d = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=days_ago)).isoformat()
            cur.execute(f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) VALUES ({m.PH},{m.PH},{m.PH},'x')",
                        ("t-stat", d, b))
        conn.commit()
    finally:
        conn.close()
    card = next(x for x in _photos()["bad"] if x["name"] == "つうさん")
    assert card["total_days"] == 51, "最初に使った日（50日前）から今日まで"
    assert card["record_rate"]["days"] == 51 and card["exercise"]["days"] == 50 and card["intake"]["days"] == 50
    assert card["avg_b_total"] == 5.0, "通算の平均B（50日前6・40日前6・1日前3）"


def _sr():
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report
    return send_report


def test_card_labels_say_total(monkeypatch):
    sr = _sr()
    monkeypatch.setattr(sr, "_decode_photo", lambda src: b"\x89PNG")
    member = {"name": "おおた", "avg_b_7d": 2.8, "avg_b_total": 2.1, "b_target": 4, "total_days": 120,
              "photos": [{"src": "x", "date": "2026-11-10", "b_count": 1}],
              "record_rate": {"days": 120, "record_days": 100, "records": 370, "records_per_day": 3.7},
              "exercise": {"days": 119, "minutes_per_day": 6, "total_min": 700, "exercise_days": 30,
                           "unknown_days": 0, "sessions": 0},
              "intake": {"days": 119, "used_days": 90, "min_meals": 2, "protein_g": 67, "veg_g": 297}}
    html = sr._photo_card(member, "t", {}, False)
    assert "通算平均 2.1 B/日" in html and "平均 2.8" not in html
    assert html.count("通算120日") == 3, "食事の記録・運動・タンパク質と野菜の3つとも通算"
    assert "直近7日" not in html


def test_section_explains_total_and_random():
    sr = _sr()
    src = open(sr.__file__, encoding="utf-8").read()
    assert "開始時からの通算の体重変化で「順調／進んでいない」に分けた" in src
    assert "進んでいない人は毎日ランダムに選んでいます" in src
    assert "直近30日の体重変化で「順調／進んでいない」" not in src
