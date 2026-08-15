# 記録漏れ対策（A：コーチAIの矛盾検知／B：会員アプリのやさしい促し／F：管理画面の要注意フラグ）
# の回帰テスト。オーナー指示 2026-08：
#   Bカウントが目標を大きく下回るのに痩せない人は「食べ過ぎ」ではなく「記録漏れ」。
#   会員向けサイトと管理画面のデータが混ざらないことも必ず担保する。

import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client():
    m.init_db()
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c


def _d(offset_days):
    return (datetime.datetime.now(m.JST) - datetime.timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _meal_payload(n_items):
    items = [{"id": 1000 + i, "result": {"total_b_count": 0.5, "total_protein_g": 5,
                                         "total_veg_g": 10, "foods": []}}
             for i in range(n_items)]
    return json.dumps({"food": {"items": items}}, ensure_ascii=False)


def _seed_member(uid, name, gender="male", goal="cut"):
    ts = _d(0) + "T08:00:00"
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute(f"DELETE FROM user_profile WHERE user_id={m.PH}", (uid,))
        cur.execute(
            f"""INSERT INTO user_profile (user_id, display_name, gender, goal, updated_at)
               VALUES ({m.PH},{m.PH},{m.PH},{m.PH},{m.PH})""",
            (uid, name, gender, goal, ts))
        conn.commit(); conn.close()


def _seed_days(uid, days_back_to_items, weights=None, b_counts=None):
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute(f"DELETE FROM daily_meals WHERE user_id={m.PH}", (uid,))
        cur.execute(f"DELETE FROM daily_weight WHERE user_id={m.PH}", (uid,))
        cur.execute(f"DELETE FROM daily_b_count WHERE user_id={m.PH}", (uid,))
        for back, n in days_back_to_items.items():
            d = _d(back)
            cur.execute(
                f"INSERT INTO daily_meals (user_id, date, payload, created_at) VALUES ({m.PH},{m.PH},{m.PH},{m.PH})",
                (uid, d, _meal_payload(n), d + "T12:00:00"))
        for back, w in (weights or {}).items():
            d = _d(back)
            cur.execute(
                f"INSERT INTO daily_weight (user_id, date, weight, created_at) VALUES ({m.PH},{m.PH},{m.PH},{m.PH})",
                (uid, d, w, d + "T08:00:00"))
        for back, b in (b_counts or {}).items():
            d = _d(back)
            cur.execute(
                f"INSERT INTO daily_b_count (user_id, date, b_count, created_at) VALUES ({m.PH},{m.PH},{m.PH},{m.PH})",
                (uid, d, b, d + "T23:00:00"))
        conn.commit(); conn.close()


# ── A：コーチAIへ渡すデータと、矛盾検知のルール ─────────────────────
def test_coach_prompt_has_underrecording_rule():
    """『Bが低いのに痩せない＝記録漏れ』の判定ルールと、減量指示の禁止が入っていること。"""
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
        src = f.read()
    assert "記録漏れの見抜き方" in src
    assert "avg_meals_per_day_7d" in src and "b_target" in src
    # 「Bをもっと減らせ」と言わせない明示的な禁止
    assert "減らしましょう" in src and "禁止" in src


def test_member_stats_include_meal_counts_and_target():
    """コーチへ渡す1人分のデータに、記録件数とB目標が含まれること。"""
    uid = "ur-stats-1"
    _seed_member(uid, "テスト太郎", gender="male")
    _seed_days(uid, {1: 1, 2: 1, 3: 1}, b_counts={1: 3.0, 2: 3.0})
    stats = [s for s in m._collect_cut_member_stats() if s["uid"] == uid]
    assert stats, "減量メンバーとして集計されていない"
    s = stats[0]
    assert s["b_target"] == 6                      # 男性・減量は6回以内
    assert s["avg_meals_per_day_7d"] is not None
    assert s["avg_meals_per_day_7d"] < 2           # 1日1件しか記録していない


# ── B：会員本人用の軽量API（自分のデータだけ） ──────────────────────
def test_my_meal_counts_returns_own_counts_only(client):
    """自分の記録件数だけを返し、他人のデータは一切含まない。"""
    me, other = "ur-me", "ur-other"
    _seed_member(me, "本人"); _seed_member(other, "別人")
    _seed_days(me, {1: 1, 2: 1})
    _seed_days(other, {1: 5, 2: 5})
    r = client.get(f"/api/my-meal-counts?user_id={me}&days=3")
    assert r.status_code == 200
    days = r.get_json()["days"]
    assert days and all(x["items"] == 1 for x in days), days   # 他人の5件が混ざらない


def test_my_meal_counts_has_no_photos(client):
    """通信を軽くするため、写真(base64)は返さない。"""
    uid = "ur-nophoto"
    _seed_member(uid, "本人"); _seed_days(uid, {1: 2})
    body = client.get(f"/api/my-meal-counts?user_id={uid}&days=3").get_data(as_text=True)
    assert "previewSrc" not in body and "base64" not in body


def test_my_meal_counts_excludes_today(client):
    """今日はまだ記録の途中なので判定に含めない。"""
    uid = "ur-today"
    _seed_member(uid, "本人")
    _seed_days(uid, {0: 1, 1: 1})
    days = client.get(f"/api/my-meal-counts?user_id={uid}&days=3").get_json()["days"]
    assert all(x["date"] != _d(0) for x in days)


def test_member_app_never_calls_admin_apis():
    """会員向け画面(index.html)が管理APIを呼んでいないこと（データ混在の防止）。"""
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert "/api/admin/" not in html, "会員アプリから管理APIを呼んではいけない"
    assert "attention-flags" not in html
    # 会員側は自分専用APIを使う
    assert "/api/my-meal-counts" in html


def test_underrec_notice_is_gentle():
    """記録ゼロの日は対象外（サボりを責めない）で、1日1回しか出さないこと。"""
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert "function checkUnderRecording" in html
    assert "UNDERREC_KEY" in html                     # 1日1回の抑制
    assert "x.items > 0" in html                      # 記録がある日だけを見る
    assert "closeUnderRecNotice" in html              # 閉じられる


# ── F：管理画面の要注意フラグ（管理者専用） ─────────────────────────
def test_attention_flags_requires_admin(client):
    """未認証は404（管理機能の存在を隠す既存方針）。"""
    assert client.get("/api/admin/attention-flags").status_code == 404


def test_attention_flags_classifies_under_recording(client):
    """記録は続いているのにBが目標の6割未満＆体重が減っていない人を拾う。"""
    uid = "ur-flag-under"
    _seed_member(uid, "記録漏れさん", gender="male")   # 目標6 → 6割=3.6未満が対象
    _seed_days(uid,
               {1: 1, 2: 1, 3: 1},
               weights={30: 60.0, 1: 61.6},           # +1.6kg（減っていない）
               b_counts={1: 3.0, 2: 3.0, 3: 3.0})
    d = client.get("/api/admin/attention-flags",
                   headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()
    names = [x["name"] for x in d["under_recording"]]
    assert "記録漏れさん" in names
    row = next(x for x in d["under_recording"] if x["name"] == "記録漏れさん")
    assert row["confidence"] == "high"                 # 1日1件 → 確度高
    assert row["b_target"] == 6


def test_attention_flags_separates_long_absent(client):
    """14日以上記録がない人は「長期離脱」に分類し、記録漏れ疑いには入れない。"""
    uid = "ur-flag-away"
    _seed_member(uid, "離脱さん")
    _seed_days(uid, {20: 3}, b_counts={20: 4.0})
    d = client.get("/api/admin/attention-flags",
                   headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()
    away_names = [x["name"] for x in d["long_absent"]]
    under_names = [x["name"] for x in d["under_recording"]]
    assert "離脱さん" in away_names
    assert "離脱さん" not in under_names


def test_attention_flags_skips_members_losing_weight(client):
    """ちゃんと減っている人は、Bが低くても要注意に出さない。"""
    uid = "ur-flag-ok"
    _seed_member(uid, "順調さん", gender="male")
    _seed_days(uid,
               {1: 1, 2: 1, 3: 1},
               weights={30: 62.0, 1: 60.5},           # -1.5kg（順調）
               b_counts={1: 3.0, 2: 3.0, 3: 3.0})
    d = client.get("/api/admin/attention-flags",
                   headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()
    assert "順調さん" not in [x["name"] for x in d["under_recording"]]
