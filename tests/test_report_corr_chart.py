# デイリーレポートの「減量希望者：平均Bカウント × 体重の変化」まわりの回帰テスト。
# オーナー指示 2026-08：
#   ・縦軸を「体重の変化」、横軸を「平均Bカウント」にする（横軸が日付では意味が読めない）
#   ・その下の「運動によるBカウント消費 集団平均」の棒グラフは不要なので削除する
# オーナー指示 2026-10-17：この散布図（chart_cut_corr）も「いらないので消して」で削除した。
#   サーバーの cut_corr（1人1点のデータ）は残っているので、そのデータの検査は残す。

import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(ROOT, "report", "send_report.py")


def _report_src():
    with open(REPORT, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def client():
    m.init_db()
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c


def _d(back):
    return (datetime.datetime.now(m.JST) - datetime.timedelta(days=back)).strftime("%Y-%m-%d")


def _seed_cut_member(uid, name, b_by_day, weights):
    """減量希望の会員1人ぶんのBカウントと体重を入れる。"""
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        for t in ("user_profile", "daily_b_count", "daily_weight"):
            cur.execute(f"DELETE FROM {t} WHERE user_id={m.PH}", (uid,))
        cur.execute(
            f"""INSERT INTO user_profile (user_id, display_name, gender, goal, updated_at)
                VALUES ({m.PH},{m.PH},{m.PH},{m.PH},{m.PH})""",
            (uid, name, "male", "cut", _d(0) + "T08:00:00"))
        for back, b in b_by_day.items():
            cur.execute(
                f"INSERT INTO daily_b_count (user_id, date, b_count, created_at) "
                f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, _d(back), b, _d(back) + "T23:00:00"))
        for back, w in weights.items():
            cur.execute(
                f"INSERT INTO daily_weight (user_id, date, weight, created_at) "
                f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, _d(back), w, _d(back) + "T08:00:00"))
        conn.commit(); conn.close()


# ── サーバー：散布図用の「1人1点」データ ──────────────────────────
def test_report_data_has_per_member_points(client):
    """会員ごとの『平均Bカウント』と『体重の変化』が返ること。"""
    _seed_cut_member("corr-a", "減った人", {1: 2.0, 2: 3.0, 3: 4.0}, {10: 70.0, 1: 68.0})
    d = client.get("/api/admin/report-data",
                   headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()
    members = d["cut_corr"]["members"]
    row = next(x for x in members if x["avg_b"] == 3.0)   # (2+3+4)/3
    # 70kg → 68kg は「−2.0kg の変化」（マイナス＝減量）
    assert row["change_kg"] == -2.0
    assert row["days"] == 3


def test_weight_change_is_negative_when_losing(client):
    """減った人はマイナス、増えた人はプラスになること（符号の取り違え防止）。"""
    _seed_cut_member("corr-up", "増えた人", {1: 5.0, 2: 5.0, 3: 5.0}, {10: 60.0, 1: 61.5})
    d = client.get("/api/admin/report-data",
                   headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()
    row = next(x for x in d["cut_corr"]["members"] if x["avg_b"] == 5.0)
    assert row["change_kg"] == 1.5


def test_members_with_few_records_are_excluded(client):
    """Bの記録が少なすぎる人は平均がぶれるので除外すること。"""
    _seed_cut_member("corr-few", "記録が少ない人", {1: 9.0}, {10: 70.0, 1: 69.0})
    d = client.get("/api/admin/report-data",
                   headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()
    assert d["cut_corr"]["min_days"] >= 2
    assert not [x for x in d["cut_corr"]["members"] if x["avg_b"] == 9.0]


def test_members_without_two_weights_are_excluded(client):
    """体重が1回しか無い人は変化を出せないので除外すること。"""
    _seed_cut_member("corr-1w", "体重1回だけ", {1: 2.5, 2: 2.5, 3: 2.5}, {1: 70.0})
    d = client.get("/api/admin/report-data",
                   headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()
    assert not [x for x in d["cut_corr"]["members"] if x["avg_b"] == 2.5]


def test_report_data_needs_admin(client):
    """会員向けには出さない（管理用データなので未認証は404）。"""
    assert client.get("/api/admin/report-data").status_code == 404


# ── レポート：散布図と運動グラフの削除 ───────────────────────────
def test_cut_corr_chart_is_removed():
    """オーナー指示 2026-10-17：「減量希望者：平均Bカウント × 体重の変化」の散布図は載せない。
    関数・本文・添付のどれにも残っていないこと（復活させないこと）。"""
    src = _report_src()
    assert "def chart_cut_corr(" not in src, "散布図の関数が復活しています"
    assert "cid:chart_cut_corr" not in src, "散布図が本文に復活しています"
    assert '"chart_cut_corr"' not in src, "散布図を生成しています"
    assert "「平均Bカウント」と「体重の変化」の相関" not in src


def test_exercise_bar_chart_is_removed():
    """『運動によるBカウント消費 集団平均』の棒グラフを消したこと。"""
    src = _report_src()
    assert "def chart_exercise" not in src, "運動グラフの関数が残っている"
    assert "chart_exercise" not in src, "運動グラフの参照が残っている"
    assert "運動によるBカウント消費 集団平均" not in src


# ── 痩せていない人の朝・昼・晩の記録状況（オーナー指示 2026-08） ──────────
def _seed_usage(uid, hours_by_day):
    """usage_log（解析の実行ログ）に時刻付きで記録を入れる。"""
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute(f"DELETE FROM usage_log WHERE user_id={m.PH}", (uid,))
        for back, hours in hours_by_day.items():
            for h in hours:
                cur.execute(
                    f"INSERT INTO usage_log (user_id, created_at) VALUES ({m.PH},{m.PH})",
                    (uid, f"{_d(back)}T{h:02d}:30:00"))
        conn.commit(); conn.close()


def _fetch(client):
    return client.get("/api/admin/report-data",
                      headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()["cut_corr"]


def test_slot_counts_are_split_by_time_of_day(client):
    """朝・昼・晩それぞれで「記録できた日数」を数えること。"""
    uid = "slot-a"
    _seed_cut_member(uid, "朝が抜ける人", {1: 4.0, 2: 4.0, 3: 4.0}, {20: 70.0, 1: 72.5})
    # 3日間、昼(12時)と晩(19時)だけ記録。朝は無し。
    _seed_usage(uid, {1: [12, 19], 2: [12, 19], 3: [12]})
    row = next(x for x in _fetch(client)["gained_members"] if x["name"] == "朝が抜ける人")
    assert row["morning_days"] == 0, "朝の記録が無いのに0になっていない"
    assert row["noon_days"] == 3
    assert row["night_days"] == 2


def test_same_slot_twice_a_day_counts_as_one_day(client):
    """1日に同じ時間帯で2回記録しても「1日」と数えること。"""
    uid = "slot-dup"
    _seed_cut_member(uid, "昼に2回の人", {1: 4.0, 2: 4.0, 3: 4.0}, {20: 70.0, 1: 72.5})
    _seed_usage(uid, {1: [11, 12, 13]})
    row = next(x for x in _fetch(client)["gained_members"] if x["name"] == "昼に2回の人")
    assert row["noon_days"] == 1


def test_late_night_counts_as_night(client):
    """深夜1時の記録は「晩」として数えること（夜食を朝に混ぜない）。"""
    uid = "slot-late"
    _seed_cut_member(uid, "夜更かしの人", {1: 4.0, 2: 4.0, 3: 4.0}, {20: 70.0, 1: 72.5})
    _seed_usage(uid, {1: [1], 2: [23]})
    row = next(x for x in _fetch(client)["gained_members"] if x["name"] == "夜更かしの人")
    assert row["night_days"] == 2
    assert row["morning_days"] == 0


def test_picks_at_most_ten_gained_members(client):
    """増えた順に、最大10名だけ載せること。"""
    for i in range(13):
        uid = f"slot-many-{i}"
        # 全員 +2kg 以上ふえている（+2.0 〜 +5.6kg）
        _seed_cut_member(uid, f"人{i:02d}", {1: 3.0, 2: 3.0, 3: 3.0},
                         {20: 70.0, 1: 72.0 + i * 0.3})
        _seed_usage(uid, {1: [8]})
    cc = _fetch(client)
    nl = cc["gained_members"]
    assert len(nl) == 10, f"10名ちょうどでない: {len(nl)}"
    # いちばん増えた人が先頭（降順）
    assert nl == sorted(nl, key=lambda z: -z["change_kg"])
    assert cc["slot_days"] >= 7


def test_slot_charts_are_removed():
    """ふえた人・へった人の「朝・昼・晩の記録状況」の表はメールに載せない。"""
    # オーナー指示 2026-10-01：「体重が2kg以上ふえた人／へった人の朝昼晩の表」と
    # 「記録が足りない会員」の表は「いらない。今後は記載するな」。復活させないこと。
    src = _report_src()
    for name in ("chart_gained_slots", "chart_lost_slots", "chart_thin_slots"):
        assert f"def {name}(" not in src, f"{name} が復活しています"
        assert f"cid:{name}" not in src, f"{name} が本文に復活しています"
        assert f'"{name}"' not in src, f"{name} を生成しています"
    assert "def _slot_table" not in src
    assert "記録漏れを探す" not in src and "うまくいっている人の記録のしかた" not in src


