# デイリーレポートの参加者カードに「運動で相殺したB」を出す回帰テスト（オーナー指示 2026-10-15）。
#
# 「運動している場合はマイナスで相殺した分もデイリーレポートにアップして」。
# 見出しの「平均 ◯ B/日」は daily_b_count（食事のB − 運動のB）の平均なので、
#   ・カードに「🏃 運動：1日平均 −◯ B を相殺（食事だけなら ◯ → 運動を引いて ◯）」
#   ・写真の「この日の合計 B◯」の下に「🏃運動 −◯ → B◯」
# を出す。運動の記録が無い会員・日には出さない。
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
import app as m  # noqa: E402
from test_report_photos import ADMIN, _clear_rotation, _reset_db, _seed_member  # noqa: E402


def _exercise(uid, days_ago, ex):
    d = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=days_ago)).isoformat()
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO daily_exercise (user_id, date, ex_b_count, created_at) "
                f"VALUES ({m.PH},{m.PH},{m.PH},'x')", (uid, d, ex))
    conn.commit()
    conn.close()


@pytest.fixture
def db():
    m.init_db()

    def clean():
        _reset_db()
        _clear_rotation()
        conn = m._get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM daily_exercise")
        conn.commit()
        conn.close()
    clean()
    yield
    clean()


def _member(uid):
    return next(x for x in m._collect_cut_member_stats() if x["uid"] == uid)


def test_member_stats_have_exercise_offset(db):
    _seed_member("ex1", "運動する人", 60, 58)        # 昨日 B3（相殺後）・写真2枚（B2ずつ）
    _exercise("ex1", 1, 1.5)
    _exercise("ex1", 3, 2.0)                          # Bの記録が無い日の運動は平均に入れない
    mem = _member("ex1")
    assert mem["avg_b_7d"] == 3.0
    assert mem["ex_b_7d"] == pytest.approx(1.5)


def test_no_exercise_means_zero(db):
    _seed_member("ex2", "運動しない人", 60, 58)
    assert _member("ex2")["ex_b_7d"] == 0.0


def test_report_photos_carry_offsets(db):
    _seed_member("ex3", "順調な人", 62, 58)
    _exercise("ex3", 1, 1.0)
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        d = c.get("/api/admin/report-photos", headers=ADMIN).get_json()
    card = next(x for x in d["good"] if x["name"] == "順調な人")
    assert card["ex_b_7d"] == pytest.approx(1.0)
    assert card["photos"][0]["day_b_count"] == 4.0
    assert card["photos"][0]["day_ex_b"] == 1.0


# ── メール本文 ───────────────────────────────────────────
def _sr():
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report
    return send_report


def test_exercise_line_shows_offset_and_food_only():
    sr = _sr()
    line = sr._exercise_line({"avg_b_7d": 4.2, "ex_b_7d": 0.8})
    assert "運動" in line and "−0.8 B" in line
    assert "食事だけなら 5.0 B/日" in line and "運動を引いて 4.2 B/日" in line


def test_exercise_line_hidden_without_exercise():
    sr = _sr()
    assert sr._exercise_line({"avg_b_7d": 4.2, "ex_b_7d": 0}) == ""
    assert sr._exercise_line({"avg_b_7d": 4.2}) == ""


def test_photo_card_shows_day_offset(monkeypatch):
    sr = _sr()
    monkeypatch.setattr(sr, "_decode_photo", lambda src: b"\x89PNG")
    member = {"name": "Uchino", "change_30d_kg": 1.0, "avg_b_7d": 4.2, "ex_b_7d": 0.8, "b_target": 4,
              "photos": [{"src": "x", "date": "2026-10-14", "b_count": 2, "day_b_count": 5, "day_ex_b": 1},
                         {"src": "x", "date": "2026-10-13", "b_count": 2, "day_b_count": 4, "day_ex_b": None}]}
    html = sr._photo_card(member, "t", {}, good=False)
    assert "🏃 運動：" in html
    assert "この日の合計 B5" in html and "🏃運動 −1 → B4" in html
    assert html.count("🏃運動 −") == 1, "運動していない日にまで相殺の表示が出ている"
