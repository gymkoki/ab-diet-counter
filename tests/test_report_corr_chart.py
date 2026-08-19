# デイリーレポートの「減量希望者：平均Bカウント × 体重の変化」グラフの回帰テスト。
# オーナー指示 2026-08：
#   ・縦軸を「体重の変化」、横軸を「平均Bカウント」にする（横軸が日付では意味が読めない）
#   ・その下の「運動によるBカウント消費 集団平均」の棒グラフは不要なので削除する

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


def _load_send_report():
    """レポート本体を読み込む。グラフ描画用のライブラリ（matplotlib / requests）は
    アプリ本体には不要で、GitHub Actionsのテストにも入れていない。
    入っていない環境では、この描画テストだけを飛ばす（他のテストは文面で担保する）。"""
    pytest.importorskip("requests", reason="レポート送信用の依存（テスト環境には未導入）")
    pytest.importorskip("matplotlib", reason="グラフ描画用の依存（テスト環境には未導入）")
    sys.path.insert(0, os.path.join(ROOT, "report"))
    import send_report
    return send_report


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


# ── レポート：グラフの軸と、運動グラフの削除 ───────────────────────
def test_chart_axes_are_bcount_and_weight_change():
    """横軸＝平均Bカウント、縦軸＝体重の変化になっていること。"""
    src = _report_src()
    assert 'ax.set_xlabel("平均Bカウント' in src, "横軸が平均Bカウントになっていない"
    assert 'ax.set_ylabel("体重の変化' in src, "縦軸が体重の変化になっていない"
    # 日付を横軸にした古い形（twinx の2軸折れ線）に戻っていないこと
    fn = src[src.index("def chart_cut_corr"):src.index("def chart_nutrition")]
    assert "twinx" not in fn, "横軸が日付の2軸グラフに戻っている"
    assert "ax.scatter" in fn, "散布図になっていない"


def test_chart_shows_correlation_and_trend_line():
    """傾向線と相関係数を出し、Bを抑えた人ほど減っているかを読み取れること。"""
    fn = _report_src()
    assert "傾向線" in fn
    assert "相関 r" in fn


def test_exercise_bar_chart_is_removed():
    """『運動によるBカウント消費 集団平均』の棒グラフを消したこと。"""
    src = _report_src()
    assert "def chart_exercise" not in src, "運動グラフの関数が残っている"
    assert "chart_exercise" not in src, "運動グラフの参照が残っている"
    assert "運動によるBカウント消費 集団平均" not in src


def test_chart_renders_without_error():
    """実際にPNGを生成できること（データあり・なしの両方）。"""
    R = _load_send_report()
    members = [{"avg_b": 2.0 + i * 0.4, "change_kg": 0.3 * i - 1.0, "days": 10}
               for i in range(6)]
    png = R.chart_cut_corr({"cut_corr": {"users": 8, "min_days": 3, "members": members},
                            "dates": []})
    assert png[:4] == b"\x89PNG"
    empty = R.chart_cut_corr({"cut_corr": {"users": 0, "min_days": 3, "members": []},
                              "dates": []})
    assert empty[:4] == b"\x89PNG"


def test_chart_handles_single_member():
    """1人しかいなくても（傾向線が引けなくても）エラーにならないこと。"""
    R = _load_send_report()
    png = R.chart_cut_corr({"cut_corr": {"users": 1, "min_days": 3,
                                         "members": [{"avg_b": 3.0, "change_kg": -1.0, "days": 5}]},
                            "dates": []})
    assert png[:4] == b"\x89PNG"


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
    _seed_cut_member(uid, "朝が抜ける人", {1: 4.0, 2: 4.0, 3: 4.0}, {20: 70.0, 1: 71.5})
    # 3日間、昼(12時)と晩(19時)だけ記録。朝は無し。
    _seed_usage(uid, {1: [12, 19], 2: [12, 19], 3: [12]})
    row = next(x for x in _fetch(client)["no_loss_members"] if x["name"] == "朝が抜ける人")
    assert row["morning_days"] == 0, "朝の記録が無いのに0になっていない"
    assert row["noon_days"] == 3
    assert row["night_days"] == 2


def test_same_slot_twice_a_day_counts_as_one_day(client):
    """1日に同じ時間帯で2回記録しても「1日」と数えること。"""
    uid = "slot-dup"
    _seed_cut_member(uid, "昼に2回の人", {1: 4.0, 2: 4.0, 3: 4.0}, {20: 70.0, 1: 71.0})
    _seed_usage(uid, {1: [11, 12, 13]})
    row = next(x for x in _fetch(client)["no_loss_members"] if x["name"] == "昼に2回の人")
    assert row["noon_days"] == 1


def test_late_night_counts_as_night(client):
    """深夜1時の記録は「晩」として数えること（夜食を朝に混ぜない）。"""
    uid = "slot-late"
    _seed_cut_member(uid, "夜更かしの人", {1: 4.0, 2: 4.0, 3: 4.0}, {20: 70.0, 1: 71.0})
    _seed_usage(uid, {1: [1], 2: [23]})
    row = next(x for x in _fetch(client)["no_loss_members"] if x["name"] == "夜更かしの人")
    assert row["night_days"] == 2
    assert row["morning_days"] == 0


def test_picks_at_most_ten_worst_members(client):
    """痩せていない順に、最大10名だけ載せること。"""
    for i in range(13):
        uid = f"slot-many-{i}"
        _seed_cut_member(uid, f"人{i:02d}", {1: 3.0, 2: 3.0, 3: 3.0},
                         {20: 70.0, 1: 70.0 + i * 0.3})
        _seed_usage(uid, {1: [8]})
    cc = _fetch(client)
    nl = cc["no_loss_members"]
    assert len(nl) == 10, f"10名ちょうどでない: {len(nl)}"
    # 体重が増えた人が先頭（降順）
    assert nl == sorted(nl, key=lambda z: -z["change_kg"])
    assert nl[0]["change_kg"] >= nl[-1]["change_kg"]
    assert cc["slot_days"] >= 7


def test_slot_chart_renders():
    """朝昼晩グラフがPNGとして生成できること（人数0でも落ちない）。"""
    R = _load_send_report()
    members = [{"name": f"会員{i}", "change_kg": 1.0 - i * 0.2, "avg_b": 3.0 + i * 0.2,
                "morning_days": i, "noon_days": 14 - i, "night_days": 0} for i in range(10)]
    png = R.chart_no_loss_slots({"cut_corr": {"slot_days": 14, "no_loss_members": members}})
    assert png[:4] == b"\x89PNG"
    empty = R.chart_no_loss_slots({"cut_corr": {"slot_days": 14, "no_loss_members": []}})
    assert empty[:4] == b"\x89PNG"


def test_slot_chart_avoids_emoji():
    """グラフ内に絵文字を使わないこと（日本語フォントに無く豆腐□になる）。"""
    src = _report_src()
    fn = src[src.index("def chart_no_loss_slots"):src.index("def chart_nutrition")]
    assert "🌅" not in fn and "🌞" not in fn and "🌙" not in fn


def test_slot_chart_is_in_the_email():
    """メール本文にグラフが差し込まれていること。"""
    src = _report_src()
    assert "cid:chart_no_loss_slots" in src
    assert '"chart_no_loss_slots": chart_no_loss_slots(data)' in src
