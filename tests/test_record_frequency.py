# 「記録が足りない会員をBカウントの分析から外す」の回帰テスト。
#
# 経緯（オーナー指摘 2026-09-29）：
#   「1日の食事の写真のアップが1回、2回の人は、Bカウントが1日1回とか2回になりますが、
#     これは単に掲載をし忘れているだけであって、実態を反映していないと思います。
#     頻繁にアップしてくれない方向けに、解析の対象から除外したい」
#
# 仕様：
#   ・判定は「朝・昼・晩の3枠のうち、記録した日に平均いくつ埋まっているか」
#     （写真の枚数ではなく時間帯の数。1食で2枚撮っても1枠にしかならない）
#   ・平均が REPORT_MIN_SLOTS_PER_DAY 未満の会員は、相関グラフ（members）と
#     平均Bカウントの推移（b_avg_trend）から外す
#   ・外した人は捨てずに thin_members で返す（記録を増やしてもらう手を打てるように）
#   ・記録日が REPORT_THIN_MIN_DAYS 未満の会員は判定材料が足りないので外さない
#   ・アプリ側は「あと何食」を記録画面に出して、忘れに本人が気づけるようにする

import datetime
import os
import re
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


def _d(back):
    return (datetime.datetime.now(m.JST) - datetime.timedelta(days=back)).strftime("%Y-%m-%d")


def _seed(uid, name, hours, days=(1, 2, 3, 4, 5)):
    """減量希望の会員を1人作る。hours = その日に記録した時刻（朝4-10/昼10-16/晩16-28）"""
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        for t in ("user_profile", "daily_b_count", "daily_weight", "usage_log"):
            cur.execute(f"DELETE FROM {t} WHERE user_id={m.PH}", (uid,))
        cur.execute(
            f"""INSERT INTO user_profile (user_id, display_name, gender, goal, updated_at)
               VALUES ({m.PH},{m.PH},{m.PH},{m.PH},{m.PH})""",
            (uid, name, "male", "cut", _d(0) + "T08:00:00"))
        # 体重は初回→最新の2点（相関グラフに乗るための最低条件）
        cur.execute(
            f"INSERT INTO daily_weight (user_id, date, weight, created_at) "
            f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, _d(40), 75.0, _d(40) + "T08:00:00"))
        cur.execute(
            f"INSERT INTO daily_weight (user_id, date, weight, created_at) "
            f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, _d(1), 74.0, _d(1) + "T08:00:00"))
        for back in days:
            cur.execute(
                f"INSERT INTO daily_b_count (user_id, date, b_count, created_at) "
                f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, _d(back), 3.0, _d(back) + "T23:00:00"))
            for hr in hours:
                cur.execute(
                    f"INSERT INTO usage_log (user_id, created_at) VALUES ({m.PH},{m.PH})",
                    (uid, f"{_d(back)}T{hr:02d}:30:00"))
        conn.commit(); conn.close()


def _cc(client):
    return client.get("/api/admin/report-data",
                      headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()["cut_corr"]


def _names(rows):
    return [r["name"] for r in rows]


# ── 除外そのもの ──────────────────────────────────────────────
def test_one_meal_per_day_is_excluded(client):
    """【本題】1日1回しか記録しない人は、相関グラフから外れること。"""
    _seed("freq-one", "1日1回だけ", hours=(12,))
    cc = _cc(client)
    assert "1日1回だけ" in _names(cc["thin_members"]), \
        "1日1回の人が「記録が足りない会員」に入っていない"
    assert cc["thin_count"] >= 1


def test_three_meals_per_day_stays(client):
    """朝昼晩そろえている人は、これまでどおり分析対象に残ること。"""
    _seed("freq-three", "朝昼晩そろう", hours=(7, 12, 19))
    cc = _cc(client)
    assert "朝昼晩そろう" not in _names(cc["thin_members"]), \
        "きちんと記録している人まで除外している"


def test_threshold_is_two_slots(client):
    """しきい値は「記録した日の平均2枠」。2枠ちょうどは残し、1枠は外すこと。"""
    assert _cc(client)["min_slots_per_day"] == m.REPORT_MIN_SLOTS_PER_DAY == 2.0
    _seed("freq-two", "1日2回", hours=(12, 19))
    _seed("freq-oneb", "1日1回", hours=(19,))
    cc = _cc(client)
    assert "1日2回" not in _names(cc["thin_members"]), "2枠ちょうどは残すこと"
    assert "1日1回" in _names(cc["thin_members"]), "1枠は外すこと"


def test_many_photos_in_one_slot_is_still_thin(client):
    """同じ時間帯に何枚も撮っただけでは「そろっている」と数えないこと
    （写真の枚数で数えると、昼に5枚撮る人が優等生に見えてしまう）。"""
    _seed("freq-burst", "昼だけ5枚", hours=(11, 12, 12, 13, 13))
    cc = _cc(client)
    assert "昼だけ5枚" in _names(cc["thin_members"]), \
        "同じ時間帯の連写を複数食として数えている"


def test_too_few_days_is_not_excluded(client):
    """記録日が少なすぎる人は判定材料が足りないので外さないこと
    （入会直後の人を「記録が足りない」と決めつけない）。"""
    assert m.REPORT_THIN_MIN_DAYS == 3
    _seed("freq-new", "入会したばかり", hours=(12,), days=(1, 2))
    cc = _cc(client)
    assert "入会したばかり" not in _names(cc["thin_members"])


# ── 外した人の扱い ────────────────────────────────────────────
def test_excluded_members_are_still_listed(client):
    """外した人は消さずに一覧で返すこと（記録を増やしてもらう手が打てるように）。"""
    _seed("freq-list", "声かけ対象", hours=(12,))
    row = next(r for r in _cc(client)["thin_members"] if r["name"] == "声かけ対象")
    assert row["slots_per_day"] == 1.0, f"1日あたりの記録枠数が入っていない: {row}"
    for key in ("morning_days", "noon_days", "night_days", "change_kg", "avg_b"):
        assert key in row, f"{key} が無いと声かけの中身を決められない: {row}"


def test_excluded_from_b_average_trend(client):
    """平均Bカウントの推移からも外すこと（数字が実態より良く出るのを防ぐ）。"""
    # そろえている人＝B6.0／記録が足りない人＝B1.0。外れていれば平均は6.0のまま。
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute(f"DELETE FROM daily_b_count")
        cur.execute(f"DELETE FROM usage_log")
        cur.execute(f"DELETE FROM user_profile")
        cur.execute(f"DELETE FROM daily_weight")
        conn.commit(); conn.close()
    _seed("freq-good", "そろえる人", hours=(7, 12, 19))
    _seed("freq-bad", "足りない人", hours=(12,))
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute(f"UPDATE daily_b_count SET b_count=6.0 WHERE user_id={m.PH}", ("freq-good",))
        cur.execute(f"UPDATE daily_b_count SET b_count=1.0 WHERE user_id={m.PH}", ("freq-bad",))
        conn.commit(); conn.close()
    cc = _cc(client)
    vals = [v for v in cc["b_avg_trend"] if v is not None]
    assert vals, "平均Bカウントの推移が空になっている"
    assert all(abs(v - 6.0) < 0.01 for v in vals), \
        f"記録が足りない人のB1.0が平均に混ざっている: {vals}"
    assert "足りない人" not in [x.get("name") for x in cc.get("members") or []]


# ── レポート側 ────────────────────────────────────────────────
def test_report_no_longer_has_thin_logger_table():
    """「記録が足りない会員」の表はメールに載せない（オーナー指示 2026-10-01）。
    ※Bカウントの分析（相関・平均B）から外す処理はサーバー側に残っている（上のテストで確認）。"""
    # オーナー指示 2026-10-01：「体重が2kg以上ふえた人／へった人の朝昼晩の表」と
    # 「記録が足りない会員」の表は「いらない。今後は記載するな」。復活させないこと。
    with open(os.path.join(ROOT, "report", "send_report.py"), encoding="utf-8") as f:
        src = f.read()
    assert "def chart_thin_slots" not in src
    assert "cid:chart_thin_slots" not in src
    assert "記録が足りない会員（" not in src


# ── アプリ側（記録し忘れに気づける仕掛け） ──────────────────────
def test_app_shows_remaining_meals():
    """記録画面に「あと何食」を出していること。"""
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert 'id="meal-progress"' in html
    assert "function renderMealProgress" in html
    assert "MEAL_TARGET_PER_DAY = 3" in html, "目標は朝・昼・晩の3回"
    assert "renderMealProgress(allItems.length)" in html, \
        "記録を足した／消したときに更新されないと数字が古いまま残る"


def test_app_counts_in_kai_not_shoku():
    """単位は「食」ではなく「回」（オーナー指示 2026-09-29）。"""
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert '<span class="mp-count" id="mp-count">0/3回</span>' in html
    body = html[html.index("function renderMealProgress"):]
    body = body[:body.index("\nfunction updateDailyTotal")]
    assert "${MEAL_TARGET_PER_DAY}回" in body
    assert "あと${rest}回分" in body
    assert "食`" not in body and "食。" not in body and "食分" not in body, \
        "「食」が残っています（単位は「回」）"


def test_app_first_step_message():
    """記録ゼロのときの案内文（オーナー指定の文面）。"""
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert "まだ記録がありません。写真を追加して、1回目の食事から始めましょう。" in html


def test_app_progress_does_not_use_admin_api():
    """会員アプリが管理画面のデータを触らないこと（一般と管理の分離）。"""
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    body = html[html.index("function renderMealProgress"):]
    body = body[:body.index("\nfunction updateDailyTotal")]
    assert "/api/admin/" not in body


def test_app_message_is_not_blaming():
    """責める文言にしないこと（記録を増やしてほしいのに逆効果になる）。"""
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    body = html[html.index("function renderMealProgress"):]
    body = body[:body.index("\nfunction updateDailyTotal")]
    for word in ("サボ", "怠け", "守れていません", "違反"):
        assert word not in body, f"責める表現が入っています: {word}"


def test_index_js_has_no_syntax_error():
    """index.html は全JSが1ファイルに入っているため、構文エラー1つでアプリ全体が止まる。"""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node が無い環境")
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    js = "\n".join(re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, re.S))
    p = subprocess.run(["node", "--check", "-"], input=js, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
