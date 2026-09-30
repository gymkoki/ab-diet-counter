"""デイリーレポートのピックアップ会員に「開始時 → 現在」の推移を出す機能の回帰テスト。

オーナー指示 2026-09-30：
  デイリーレポートでピックアップされた各参加者が、開始前と比べて
  現在どういう状態になっているか、その推移が分かるように表示する。

守りたい不変条件：
  ①「開始」は最初に体重を記録した日。開始時・現在の体重と変化量・変化率が正しい
  ②食べ方の変化（開始直後7日 vs 直近7日の平均B）が出る。始めたばかりで期間が重なる人は比較しない
  ③ピックアップの各会員に progress が付く（写真APIの応答に含まれる）
  ④メールでは推移グラフが添付され、本文から参照されている（空枠にならない）
  ⑤体重が1回しか無い人・推移が無い人でもレポートが落ちない
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
import app as m  # noqa: E402
from test_report_photos import ADMIN, _reset_db, _seed_member  # noqa: E402

UID = "prog-user"


def _today():
    return datetime.datetime.now(m.JST).date()


def _put(uid, weights=(), bcounts=()):
    """weights / bcounts は [(何日前, 値), ...]。"""
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM daily_weight WHERE user_id={m.PH}", (uid,))
        cur.execute(f"DELETE FROM daily_b_count WHERE user_id={m.PH}", (uid,))
        for days, w in weights:
            d = (_today() - datetime.timedelta(days=days)).isoformat()
            cur.execute(f"INSERT INTO daily_weight (user_id,date,weight,created_at) "
                        f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, d, w, d))
        for days, b in bcounts:
            d = (_today() - datetime.timedelta(days=days)).isoformat()
            cur.execute(f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) "
                        f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, d, b, d))
        conn.commit()
    finally:
        conn.close()


def _progress(uid):
    conn = m._get_conn()
    try:
        return m._member_progress(conn.cursor(), uid)
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def db():
    m.init_db()
    _reset_db()
    yield
    _reset_db()


# ── ① 開始時 → 現在 ─────────────────────────────────────────────
def test_start_and_current_weight():
    _put(UID, weights=[(90, 70.0), (60, 69.1), (30, 68.0), (1, 66.5)])
    p = _progress(UID)
    assert p["start_weight"] == 70.0
    assert p["latest_weight"] == 66.5
    assert p["total_change_kg"] == -3.5
    assert p["total_change_pct"] == -5.0
    assert p["days_since_start"] == 89
    assert p["start_date"] == (_today() - datetime.timedelta(days=90)).isoformat()
    assert len(p["weights"]) == 4, "推移グラフ用に全期間の体重が要る"


def test_no_weight_means_no_progress():
    _put(UID, weights=[], bcounts=[(1, 3.0)])
    assert _progress(UID) is None


# ── ② 食べ方の変化 ──────────────────────────────────────────────
def test_eating_change_first_week_vs_last_week():
    # 開始直後の7日は B=7、直近7日は B=3
    b = [(60 - i, 7.0) for i in range(7)] + [(i, 3.0) for i in range(7)]
    _put(UID, weights=[(60, 70.0), (1, 68.0)], bcounts=b)
    p = _progress(UID)
    assert p["b_first_week_avg"] == 7.0
    assert p["b_last_week_avg"] == 3.0
    assert len(p["b_daily"]) == 14


def test_new_member_does_not_compare_overlapping_weeks():
    """始めて数日の人は「開始直後」と「直近」が同じ期間になるので比較を出さない。"""
    _put(UID, weights=[(3, 60.0), (0, 59.8)], bcounts=[(i, 4.0) for i in range(4)])
    p = _progress(UID)
    assert p["b_first_week_avg"] is None
    assert p["b_last_week_avg"] == 4.0


# ── ③ ピックアップの各会員に付く ─────────────────────────────────
def test_report_photos_include_progress():
    _seed_member("u_good", "がんばり子", 70.0, 67.5)
    _seed_member("u_bad", "ていたい男", 70.0, 71.0)
    c = m.app.test_client()
    d = c.get("/api/admin/report-photos", headers=ADMIN).get_json()
    for group in ("good", "bad"):
        assert d[group], f"{group} が空です"
        for mem in d[group]:
            p = mem.get("progress")
            assert p, f"{mem['name']} に推移が付いていません"
            assert p["start_weight"] == 70.0
    assert d["good"][0]["progress"]["total_change_kg"] == -2.5
    assert d["bad"][0]["progress"]["total_change_kg"] == 1.0


# ── ④⑤ メールの表示 ─────────────────────────────────────────────
def _member(progress):
    from test_report_photos import PHOTO
    return {"name": "テスト", "change_30d_kg": -1.2, "avg_b_7d": 3.0, "b_target": 4,
            "photos": [{"src": PHOTO, "date": "2026-09-29", "b_count": 2, "foods": "白米"}],
            "progress": progress}


def test_email_card_shows_start_to_now_with_chart():
    pytest.importorskip("matplotlib")
    import send_report as S
    b = [(60 - i, 7.0) for i in range(7)] + [(i, 3.0) for i in range(7)]
    _put(UID, weights=[(60, 70.0), (30, 68.8), (1, 67.2)], bcounts=b)
    charts = {}
    html = S._photo_card(_member(_progress(UID)), "ph_good0", charts, True)
    assert "開始時 70.0kg" in html and "現在 67.2kg" in html
    assert "-2.8kg" in html and "-4.0%" in html
    assert "開始直後 7 → 直近" in html
    # 推移グラフが添付され、本文から参照されている（空枠にならない）
    assert "ph_good0_progress" in charts
    assert charts["ph_good0_progress"][:4] == b"\x89PNG"
    assert 'src="cid:ph_good0_progress"' in html


def test_email_card_with_single_weight():
    pytest.importorskip("matplotlib")
    import send_report as S
    _put(UID, weights=[(5, 62.0)])
    charts = {}
    html = S._photo_card(_member(_progress(UID)), "ph_bad0", charts, False)
    assert "記録が開始時の1回だけ" in html
    assert "ph_bad0_progress" in charts


def test_email_card_without_progress_still_renders():
    pytest.importorskip("matplotlib")
    import send_report as S
    charts = {}
    html = S._photo_card(_member(None), "ph_good1", charts, True)
    assert "開始時" not in html
    assert "ph_good1_progress" not in charts
    assert "cid:ph_good1_0" in html, "写真まで消えてしまっています"


def test_broken_chart_does_not_break_the_card(monkeypatch):
    pytest.importorskip("matplotlib")
    import send_report as S
    _put(UID, weights=[(30, 70.0), (1, 69.0)])

    def _boom(*a, **k):
        raise RuntimeError("描画失敗")
    monkeypatch.setattr(S, "chart_progress", _boom)
    charts = {}
    html = S._photo_card(_member(_progress(UID)), "ph_good2", charts, True)
    assert "開始時 70.0kg" in html, "グラフが失敗しても要約は出す"
    assert "ph_good2_progress" not in charts
    assert "cid:ph_good2_progress" not in html, "添付していない画像を参照すると空枠になる"
