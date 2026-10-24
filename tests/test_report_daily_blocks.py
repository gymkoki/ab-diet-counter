# デイリーレポートの写真を「1日ごと」にまとめて見せる回帰テスト（オーナー指示 2026-10-24）。
#
# 「1日ごとにB何回、食事記録何回、運動は何をしたかを、もっと1日ごとに視覚的に分かりやすく」。
#   ・写真を日ごとにまとめ、日付の見出しに 🍽B・📝記録の回数・🏃運動の内容 を色つきの札で並べる
#   ・運動の内容はアプリが /api/daily-exercise に items として送り、daily_exercise.detail に残す
#     （以前はBの合計しか送っていなかった。古い記録は「内容未登録」）
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
import app as m  # noqa: E402
from test_report_photos import ADMIN, _clear_rotation, _reset_db, _seed_member  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _day(days_ago):
    return (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=days_ago)).isoformat()


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
    m.app.config["TESTING"] = True
    yield
    clean()


def _post_ex(c, uid, date, b, items=None):
    body = {"user_id": uid, "date": date, "ex_b_count": b}
    if items is not None:
        body["items"] = items
    assert c.post("/api/daily-exercise", json=body).status_code == 200


def _detail(uid, date):
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT detail FROM daily_exercise WHERE user_id={m.PH} AND date={m.PH}", (uid, date))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


# ── 運動の内容の保存 ──────────────────────────────────────────
def test_exercise_items_are_stored(db):
    with m.app.test_client() as c:
        _post_ex(c, "u1", _day(1), 1.5, [{"type": "ウォーキング", "min": 30, "bc": 0.5},
                                         {"type": "リオールジム対面トレーニング", "bc": 1}])
    stored = json.loads(_detail("u1", _day(1)))
    assert [x["type"] for x in stored] == ["ウォーキング", "リオールジム対面トレーニング"]
    assert stored[0]["min"] == 30


def test_old_app_without_items_keeps_the_detail(db):
    """古いアプリ・他の端末（items なし）から届いても、残っている内容を消さない。"""
    with m.app.test_client() as c:
        _post_ex(c, "u2", _day(1), 1, [{"type": "階段", "floors": 5, "bc": 1}])
        _post_ex(c, "u2", _day(1), 1)
    assert json.loads(_detail("u2", _day(1)))[0]["type"] == "階段"


def test_deleting_all_exercise_clears_the_detail(db):
    with m.app.test_client() as c:
        _post_ex(c, "u3", _day(1), 1, [{"type": "ジョギング", "min": 20, "bc": 1}])
        _post_ex(c, "u3", _day(1), 0, [])
    assert json.loads(_detail("u3", _day(1))) == []


def test_clean_exercise_items_limits_input():
    assert m._clean_exercise_items(None) is None
    assert m._clean_exercise_items("x") is None
    out = json.loads(m._clean_exercise_items(
        [{"type": "あ" * 50, "min": "30", "steps": -1, "bc": True}, "bad", {"type": ""}] * 30))
    assert len(out) <= m.EXERCISE_DETAIL_MAX_ITEMS
    assert out[0] == {"type": "あ" * 20}


@pytest.mark.parametrize("item,label", [
    ({"type": "ウォーキング", "min": 30}, "ウォーキング 30分"),
    ({"type": "ウォーキング", "steps": 8000}, "ウォーキング 8,000歩"),
    ({"type": "階段", "floors": 5}, "階段 5階"),
    ({"type": "リオールジム対面トレーニング"}, "リオールジム対面トレーニング"),
])
def test_exercise_label(item, label):
    assert m._exercise_label(item) == label


# ── 日ごとの要約 ──────────────────────────────────────────────
def _meals(uid, date, bs):
    payload = {"food": {"items": [{"result": {"foods": [{"name": "x", "b_count": b}], "total_b_count": b}}
                                  for b in bs]}}
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO daily_meals (user_id,date,payload,created_at) VALUES ({m.PH},{m.PH},{m.PH},'x')",
                (uid, date, json.dumps(payload)))
    conn.commit()
    conn.close()


def _days(uid):
    conn = m._get_conn()
    try:
        return m._member_report_days(conn.cursor(), uid, _day(7))
    finally:
        conn.close()


def test_member_report_days(db):
    _meals("d1", _day(1), [1, 0.5, 1])
    _meals("d1", _day(2), [2])
    with m.app.test_client() as c:
        _post_ex(c, "d1", _day(1), 1, [{"type": "ウォーキング", "min": 30, "bc": 1}])
    days = _days("d1")
    assert [d["date"] for d in days] == [_day(1), _day(2)], "新しい日から並べる"
    y = days[0]
    assert (y["food_b"], y["ex_b"], y["net_b"], y["meal_count"]) == (2.5, 1.0, 1.5, 3)
    assert y["exercises"] == ["ウォーキング 30分"]
    assert days[1]["ex_b"] == 0 and days[1]["exercises"] is None and days[1]["meal_count"] == 1


def test_old_exercise_without_detail_is_unknown(db):
    _meals("d2", _day(1), [1])
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO daily_exercise (user_id,date,ex_b_count,created_at) VALUES ({m.PH},{m.PH},1,'x')",
                ("d2", _day(1)))
    conn.commit()
    conn.close()
    d = _days("d2")[0]
    assert d["ex_b"] == 1 and d["exercises"] is None


def test_report_photos_carry_days(db):
    _seed_member("d3", "まとめ子", 62, 58)
    with m.app.test_client() as c:
        d = c.get("/api/admin/report-photos", headers=ADMIN).get_json()
    card = next(x for x in d["good"] if x["name"] == "まとめ子")
    assert card["days"] and card["days"][0]["meal_count"] == 2


# ── メール本文 ───────────────────────────────────────────────
def _sr():
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report
    return send_report


def test_day_header_chips():
    sr = _sr()
    h = sr._day_header({"date": "2026-10-21", "food_b": 2.5, "ex_b": 1, "net_b": 1.5, "meal_count": 4,
                        "exercises": ["ウォーキング 30分", "リオールジム対面トレーニング"]}, 4)
    assert "10/21（水）" in h
    assert "🍽 B2.5 − 🏃1 = B1.5（目標4）" in h and "#047857" in h      # 目標以内＝緑
    assert "📝 記録 4回" in h
    assert "🏃 ウォーキング 30分・リオールジム対面トレーニング（−1B）" in h

    over = sr._day_header({"date": "2026-10-20", "food_b": 6, "ex_b": 0, "net_b": 6, "meal_count": 1,
                           "exercises": None}, 4)
    assert "🍽 B6（目標4）" in over and "#B91C1C" in over                # 目標超え・記録1回＝赤
    assert "🏃 運動なし" in over

    unknown = sr._day_header({"date": "2026-10-19", "food_b": 3, "ex_b": 1, "net_b": 2, "meal_count": 2,
                              "exercises": None}, None)
    assert "🏃 内容未登録（−1B）" in unknown and "#92400E" in unknown       # 記録2回＝黄


def test_photo_card_groups_photos_by_day(monkeypatch):
    sr = _sr()
    monkeypatch.setattr(sr, "_decode_photo", lambda src: b"\x89PNG")
    member = {"name": "まとめ子", "change_30d_kg": -1.0, "avg_b_7d": 2, "b_target": 4,
              "photos": [{"src": "x", "date": "2026-10-21", "b_count": 1, "day_b_count": 2.5, "meal_slot": "朝"},
                         {"src": "x", "date": "2026-10-21", "b_count": 1.5, "day_b_count": 2.5, "meal_slot": "昼"},
                         {"src": "x", "date": "2026-10-19", "b_count": 2, "day_b_count": 2}],
              "days": [{"date": "2026-10-21", "food_b": 2.5, "ex_b": 1, "net_b": 1.5, "meal_count": 3,
                        "exercises": ["ウォーキング 30分"]},
                       {"date": "2026-10-20", "food_b": 3, "ex_b": 0, "net_b": 3, "meal_count": 2, "exercises": None},
                       {"date": "2026-10-19", "food_b": 2, "ex_b": 0, "net_b": 2, "meal_count": 1, "exercises": None}]}
    html = sr._photo_card(member, "t", {}, True)
    i21, i20, i19 = html.index("10/21（水）"), html.index("10/20（火）"), html.index("10/19（月）")
    assert i21 < i20 < i19, "新しい日から順に並べる"
    assert html.count("cid:t_") == 3 and "掲載 3枚" in html
    assert i21 < html.index("cid:t_0") < html.index("cid:t_1") < i20 < i19 < html.index("cid:t_2")
    assert "写真なし" in html[i20:i19], "写真の無い日も要約を出す"
    assert "この日の合計" not in html, "日の合計は見出しに出すので写真ごとには繰り返さない"


def test_photo_card_without_days_keeps_old_layout(monkeypatch):
    """古いサーバー（days を返さない）でもこれまでどおり表示する。"""
    sr = _sr()
    monkeypatch.setattr(sr, "_decode_photo", lambda src: b"\x89PNG")
    member = {"name": "旧", "photos": [{"src": "x", "date": "2026-10-21", "b_count": 1, "day_b_count": 2.5}]}
    html = sr._photo_card(member, "t", {}, True)
    assert "この日の合計 B2.5" in html and "（水）" not in html


# ── アプリ：その日の運動の内容を送る ─────────────────────────────
@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_app_sends_exercise_items():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    import re
    fn = re.search(r"function _exerciseItemsForDate\(date, exBCount\) \{.*?\n\}", html, re.S).group(0)
    assert "body.items = items" in html
    logs = [{"date": "2026-10-21", "type": "ウォーキング", "min": 30, "bc": 0.5, "kcal": 99, "ts": 1},
            {"date": "2026-10-20", "type": "階段", "floors": 5, "bc": 1, "ts": 2}]
    harness = f"""
const EXERCISE_KEY = 'k';
const localStorage = {{ getItem: () => {json.dumps(json.dumps(logs, ensure_ascii=False))} }};
{fn}
console.log(JSON.stringify([
  _exerciseItemsForDate('2026-10-21', 0.5),
  _exerciseItemsForDate('2026-10-22', 1),    // 他の端末で記録した日 → null（サーバーの内容を消さない）
  _exerciseItemsForDate('2026-10-23', 0),    // 運動なし → 空のリスト
]));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(harness)
        path = tmp.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
    finally:
        os.unlink(path)
    assert out[0] == [{"type": "ウォーキング", "min": 30, "bc": 0.5}]
    assert out[1] is None
    assert out[2] == []
