"""デイリーレポートの会員カード「3つの数字を大きく」の回帰テスト（オーナー指示 2026-11-12）。

指摘：「デイリーレポートが見えにくいので、①食事記録（1日平均何回）②運動（1日平均何分）
③平均タンパク質と野菜 を大きな文字で表示して。減量の推移はグラフでわかるので、文章の記載は消していい」。

固定する不変条件：
  ① 運動は「1日平均◯分」（直近7日・昨日まで・運動しなかった日も0分として7日で割る）。
     歩数だけの歩行は100歩＝1分、階段は数えない、時間の無い運動は回数で別に返す、内容の無い日は「内容不明」
  ② カードには3つの数字が大きな枠で並ぶ（食事の記録 → 運動 → タンパク質・野菜）
  ③ 推移の文章（開始時◯kg → 現在◯kg・食べ方の平均B）は出さず、グラフは残す
"""
import datetime
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "report"))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
import app as m  # noqa: E402

UID = "u-key-stats"


def _day(n):
    return (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=n)).isoformat()


@pytest.fixture
def cur():
    m.init_db()
    conn = m._get_conn()
    c = conn.cursor()
    c.execute(f"DELETE FROM daily_exercise WHERE user_id={m.PH}", (UID,))
    conn.commit()
    yield c, conn
    c.execute(f"DELETE FROM daily_exercise WHERE user_id={m.PH}", (UID,))
    conn.commit()
    conn.close()


def _ex(c, conn, days_ago, ex_b, items):
    c.execute(f"INSERT INTO daily_exercise (user_id, date, ex_b_count, created_at, detail) "
              f"VALUES ({m.PH},{m.PH},{m.PH},'x',{m.PH})",
              (UID, _day(days_ago), ex_b, None if items is None else json.dumps(items, ensure_ascii=False)))
    conn.commit()


# ── ① 運動の1日平均（分）────────────────────────────────────
def test_exercise_minutes_per_day(cur):
    c, conn = cur
    _ex(c, conn, 1, 0.5, [{"type": "散歩/室内歩行", "min": 30}, {"type": "筋トレ", "min": 20}])
    _ex(c, conn, 2, 0.5, [{"type": "散歩/室内歩行", "steps": 4000}])        # 4000歩＝40分
    _ex(c, conn, 3, 0.2, [{"type": "階段", "floors": 5}])                   # 階段は数えない
    _ex(c, conn, 4, 1.0, [{"type": "リオールジム対面トレーニング"}])           # 時間なし → 回数
    _ex(c, conn, 5, 0.5, None)                                              # 内容不明（導入前など）
    _ex(c, conn, 0, 0.5, [{"type": "ランニング", "min": 60}])                # 今日は入れない
    _ex(c, conn, 9, 0.5, [{"type": "ランニング", "min": 60}])                # 7日より前も入れない
    r = m._member_exercise_minutes(c, UID)
    assert r["total_min"] == 90                        # 30 + 20 + 40
    assert r["minutes_per_day"] == round(90 / 7)       # 運動しなかった日も0分として7日で割る
    assert r["sessions"] == 1 and r["unknown_days"] == 1
    assert r["exercise_days"] == 4


def test_no_exercise_is_zero(cur):
    c, _ = cur
    r = m._member_exercise_minutes(c, UID)
    assert r["minutes_per_day"] == 0 and r["exercise_days"] == 0


def test_report_photos_include_exercise():
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert '"exercise": _member_exercise_minutes(cur, mem["uid"]),' in src


# ── ②③ メールのカード ──────────────────────────────────────────
def _sr():
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report
    return send_report


def test_card_shows_three_big_numbers(monkeypatch):
    sr = _sr()
    monkeypatch.setattr(sr, "_decode_photo", lambda src: b"\x89PNG")
    member = {"name": "おおた", "avg_b_7d": 2.8, "b_target": 4,
              "photos": [{"src": "x", "date": "2026-11-10", "b_count": 1}],
              "record_rate": {"days": 7, "record_days": 7, "records": 28, "records_per_day": 4.0},
              "exercise": {"days": 7, "minutes_per_day": 25, "total_min": 175, "exercise_days": 5,
                           "unknown_days": 0, "sessions": 1},
              "intake": {"days": 7, "used_days": 7, "min_meals": 2, "protein_g": 69, "veg_g": 298}}
    html = sr._photo_card(member, "t", {}, True)
    assert html.index("📝 食事の記録") < html.index("🏃 運動") < html.index("🥗 タンパク質・野菜")
    assert ">4.0</span>" in html and ">25</span>" in html and ">69</span>" in html and ">298</span>" in html
    assert "font-size:26px" in html, "大きな文字で出ていない"
    assert "＋時間なしの運動 1回" in html
    # 以前の小さな文字の3行は出さない
    for old in ("📝 食事の記録：", "🏃 運動：", "🥗 食べた量："):
        assert old not in html


def test_progress_text_is_removed_but_chart_stays(monkeypatch):
    sr = _sr()
    monkeypatch.setattr(sr, "chart_progress", lambda progress, good: b"\x89PNG")
    progress = {"start_weight": 60.0, "latest_weight": 59.0, "start_date": "2026-08-07",
                "latest_date": "2026-11-10", "total_change_kg": -1.0, "total_change_pct": -1.7,
                "days_since_start": 95, "weight_records": 12, "b_first_week_avg": 2.6, "b_last_week_avg": 2.8}
    charts = {}
    block = sr._progress_block(progress, "p", charts, True)
    assert 'src="cid:p"' in block and "p" in charts
    for txt in ("開始時", "現在", "食べ方", "95日間"):
        assert txt not in block, f"推移の文章「{txt}」が残っている"
