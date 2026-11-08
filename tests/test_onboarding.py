"""「はじめの3ステップ」の回帰テスト（オーナー指示 2026-11-08）。

新規登録から7日以内の会員の記録画面の上部にチェックリストを出す：
  ①食事を写真で1枚記録 ②体重を記録 ③運動を1件記録
3つそろったらお祝いと、こまめポイント＋1。7日たつか、3つそろって受け取ったら出さない。

固定する不変条件：
  ① 新しい会員には出る／登録から7日以上たった会員（既存の会員）には出ない
  ② ポイントは、サーバーに写真・体重・運動の記録が届いているときだけ付く（端末の申告だけでは付かない）
  ③ ボーナスは1人1回だけ（何度受け取りに来ても +1 のまま）
  ④ ボーナスはこまめ記録ポイントの合計・10pt達成の判定・レポートの集計に入る
  ⑤ 削除して空になった運動の日は「運動を記録した」に数えない
  ⑥ 画面：記録画面の上部にカードがあり、写真・体重・運動の記録のたびに描き直す
"""
import datetime
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
import app as m  # noqa: E402

UID = "u-onboard-new"
OLD = "u-onboard-old"
ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
TABLES = ("action_log", "daily_weight", "daily_exercise", "daily_b_count", "daily_meals",
          "daily_nutrition", "usage_log", "meal_points", "meal_point_bonus")


def _wipe():
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        for t in TABLES:
            cur.execute(f"DELETE FROM {t} WHERE user_id IN ({m.PH},{m.PH})", (UID, OLD))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def clean():
    m.init_db()
    _wipe()
    yield
    _wipe()


@pytest.fixture
def client():
    return m.app.test_client()


def _today():
    return datetime.datetime.now(m.JST).date()


def _do_steps(c, uid=UID, photo=True, weight=True, exercise=True):
    d = _today().isoformat()
    if photo:
        assert c.post("/api/log-action", json={"user_id": uid, "action": "photo"}).status_code == 200
    if weight:
        assert c.post("/api/daily-weight", json={"user_id": uid, "weight": 60.5, "date": d}).status_code == 200
    if exercise:
        r = c.post("/api/daily-exercise", json={"user_id": uid, "ex_b_count": 0.5, "date": d,
                                                "items": [{"type": "散歩/室内歩行", "min": 30, "bc": 0.5}]})
        assert r.status_code == 200


# ── ① 出す相手 ─────────────────────────────────────────
def test_new_member_sees_checklist(client):
    d = client.get(f"/api/onboarding?user_id={UID}").get_json()
    assert d["show"] and d["eligible"] and not d["claimed"]
    assert d["steps"] == {"photo": False, "weight": False, "exercise": False}
    assert d["days_left"] == m.ONBOARDING_DAYS
    assert d["ends"] == (_today() + datetime.timedelta(days=m.ONBOARDING_DAYS)).isoformat()


def test_existing_member_does_not_see_it(client):
    old = (_today() - datetime.timedelta(days=m.ONBOARDING_DAYS)).isoformat()
    client.post("/api/daily-weight", json={"user_id": OLD, "weight": 70, "date": old})
    d = client.get(f"/api/onboarding?user_id={OLD}").get_json()
    assert not d["show"] and not d["eligible"], "登録から7日たった会員には出さない"


def test_day_six_still_shows_and_day_seven_hides():
    first = _today() - datetime.timedelta(days=6)
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO daily_weight (user_id, date, weight, created_at) VALUES ({m.PH},{m.PH},60,'x')",
                    (UID, first.isoformat()))
        conn.commit()
    finally:
        conn.close()
    assert m._onboarding_state(UID)["show"] is True
    assert m._onboarding_state(UID, today=_today() + datetime.timedelta(days=1))["show"] is False


# ── ② ポイントは記録が届いてから ────────────────────────────
def test_claim_refused_until_all_three_are_recorded(client):
    _do_steps(client, exercise=False)
    r = client.post("/api/onboarding/claim", json={"user_id": UID})
    assert r.status_code == 409 and r.get_json()["claimed"] is False
    assert r.get_json()["steps"] == {"photo": True, "weight": True, "exercise": False}
    assert m._bonus_points_of(UID) == 0, "そろっていないのにポイントが付いた"


def test_claim_grants_one_point_once(client):
    _do_steps(client)
    st = client.get(f"/api/onboarding?user_id={UID}").get_json()
    assert st["done"] and st["show"]
    r = client.post("/api/onboarding/claim", json={"user_id": UID})
    assert r.status_code == 200
    d = r.get_json()
    assert d["claimed"] and d["show"] is False, "受け取ったら出さない"
    assert d["meal_points"]["points"] == 1 and d["meal_points"]["bonus"] == 1
    # ③ 2回目以降も +1 のまま
    for _ in range(3):
        assert client.post("/api/onboarding/claim", json={"user_id": UID}).status_code == 200
    assert m._bonus_points_of(UID) == m.ONBOARDING_BONUS_POINTS == 1
    assert client.get(f"/api/meal-points?user_id={UID}").get_json()["points"] == 1


def test_claim_after_window_is_refused(client):
    old = (_today() - datetime.timedelta(days=10)).isoformat()
    _do_steps(client, uid=OLD)
    client.post("/api/daily-weight", json={"user_id": OLD, "weight": 70, "date": old})
    r = client.post("/api/onboarding/claim", json={"user_id": OLD})
    assert r.status_code == 409 and r.get_json()["eligible"] is False
    assert m._bonus_points_of(OLD) == 0


def test_text_record_is_not_a_photo(client):
    client.post("/api/log-action", json={"user_id": UID, "action": "text"})
    assert m._onboarding_steps(UID)["photo"] is False


# ⑤ 削除して空になった運動は数えない
def test_deleted_exercise_does_not_count(client):
    d = _today().isoformat()
    client.post("/api/daily-exercise", json={"user_id": UID, "ex_b_count": 0, "date": d, "items": []})
    assert m._onboarding_steps(UID)["exercise"] is False
    # 消費Bが0でも、内容が残っていれば記録した（短い散歩など）
    client.post("/api/daily-exercise", json={"user_id": UID, "ex_b_count": 0, "date": d,
                                             "items": [{"type": "ヨガ／ストレッチ", "min": 5, "bc": 0}]})
    assert m._onboarding_steps(UID)["exercise"] is True


# ── ④ 合計・達成判定・集計にボーナスが入る ──────────────────────
def test_bonus_counts_toward_goal_and_reports(client):
    _do_steps(client)
    client.post("/api/onboarding/claim", json={"user_id": UID})
    # 3食そろった日を9日ぶん入れると、ボーナスと合わせて10pt＝達成
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        for i in range(9):
            day = (_today() - datetime.timedelta(days=i + 1)).isoformat()
            cur.execute(f"INSERT INTO meal_points (user_id, date, created_at) VALUES ({m.PH},{m.PH},'x')", (UID, day))
        conn.commit()
    finally:
        conn.close()
    mp = m._meal_points_payload(UID)
    assert mp["points"] == m.MEAL_POINTS_GOAL and mp["unlocked"] is True
    assert len(mp["dates"]) == 9, "日付の一覧にはボーナスを混ぜない（3食そろった日の判定に使うため）"

    stats = m._mame_points_stats((_today() - datetime.timedelta(days=1)).isoformat())
    me = [t for t in stats["top"] if t["points"] == m.MEAL_POINTS_GOAL]
    assert me, "レポートの上位にボーナス込みの累計が出ていない"
    assert stats["unlocked"] >= 1

    adm = client.get("/api/admin/special-message", headers=ADMIN).get_json()
    assert adm["members_unlocked"] >= 1


def test_bonus_only_member_is_counted_as_holder(client):
    _do_steps(client)
    client.post("/api/onboarding/claim", json={"user_id": UID})
    stats = m._mame_points_stats(_today().isoformat())
    assert any(t["points"] == 1 for t in stats["top"]), "ボーナスだけの会員も「持っている人」に入る"


# ── ⑥ 画面 ──────────────────────────────────────────────
def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def test_card_is_at_top_of_record_screen():
    h = _html()
    main = h[h.index('<div id="view-main">'):]
    assert main.index('id="record-subnav"') < main.index('id="onboard"') < main.index('id="sub-food"'), \
        "記録画面の上部（食事・運動・体重の切り替えのすぐ下）に置く"


def test_steps_and_hooks():
    h = _html()
    for label in ("食事を写真で1枚記録", "体重を記録", "運動を1件記録"):
        assert label in h
    assert "はじめの3ステップ達成！" in h
    # 写真の解析・食事や運動の更新・体重の保存のたびに描き直す
    assert "if (action === 'photo') { try { markOnboardPhoto(); renderOnboarding();" in h
    upd = h[h.index("renderMealPoints();      // こまめ記録ポイント"):][:120]
    assert "renderOnboarding();" in upd
    sw = h[h.index("function saveTodayWeight()"):]
    sw = sw[:sw.index("\n}\n")]
    assert "renderOnboarding();" in sw
    # サーバーが「出す」と言うまで出さない（既存の会員に誤って出さない）
    assert re.search(r"const def = \{ show: false,", h)
