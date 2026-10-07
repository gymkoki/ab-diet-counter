"""減量の成功群と失敗群の「記録のしかた」の比較の回帰テスト（オーナー指摘 2026-10-07）。

「失敗群の方が摂取カロリーが少ないのはおかしい。写真をアップしていないのでは？
 写真の枚数や間食の枚数に違いがあるのでは？」を数字で確かめるための集計。
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

# report/send_report.py は import しない（requests・matplotlib が要り、CI には入っていないため
# 収集エラーで全テストが止まる）。中身の確認はファイルを文字列として読むだけで足りる。
REPORT_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "report", "send_report.py")


@pytest.fixture
def planted():
    """成功群6人：毎日 朝・昼・晩に写真＋15時の間食＝1日4件（写真4枚）。
    失敗群6人：2日に1回だけ、昼に1件・夜は文章で1件＝1日2件（写真1枚・間食0）。"""
    m.init_db()
    today = datetime.datetime.now(m.JST).date()
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("daily_weight", "action_log", "daily_nutrition", "user_profile"):
        cur.execute(f"DELETE FROM {t}")

    def weight(uid, slope30):
        for i in range(0, 40, 4):
            d = (today - datetime.timedelta(days=i)).isoformat()
            cur.execute(f"INSERT INTO daily_weight (user_id,date,weight,created_at) "
                        f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, d, 65 - slope30 * i / 30, d))
        cur.execute(f"INSERT INTO user_profile (user_id, goal, updated_at) VALUES ({m.PH},'cut','x')", (uid,))

    def act(uid, d, hour, action="photo"):
        ts = f"{d.isoformat()}T{hour:02d}:15:00+09:00"
        cur.execute(f"INSERT INTO action_log (user_id, action, created_at) VALUES ({m.PH},{m.PH},{m.PH})",
                    (uid, action, ts))

    for k in range(6):
        uid = f"ok-{k}"
        weight(uid, -1.5)
        for i in range(1, 21):
            d = today - datetime.timedelta(days=i)
            for h in (7, 12, 15, 19):
                act(uid, d, h)
            if i <= k:                      # 人ごとのばらつき：k日だけ夜食も記録
                act(uid, d, 22)
    for k in range(6):
        uid = f"ng-{k}"
        weight(uid, +0.5)
        for i in range(1, 21, 2):
            d = today - datetime.timedelta(days=i)
            act(uid, d, 12)
            act(uid, d, 20, "photo" if i <= 2 * k else "text")   # 人ごとのばらつき
    conn.commit()
    conn.close()
    yield
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("daily_weight", "action_log", "daily_nutrition", "user_profile"):
        cur.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()


def _by_key(res):
    return {r["key"]: r for r in res["results"]}


def test_groups_include_members_without_full_days(planted):
    """3食そろった日が1日もない人も比べる（記録漏れの人こそ見たいので）。"""
    res = m._record_behavior_compare()
    assert res["loss_n"] == 6 and res["gain_n"] == 6


def test_photo_and_snack_counts(planted):
    r = _by_key(m._record_behavior_compare())
    # 成功群：4枚＋夜食（0〜5日/20日）。失敗群：1枚＋夜の写真（0〜5日/10日）
    assert r["photos_per_day"]["loss"]["mean"] == pytest.approx(4 + 2.5 / 20)
    assert r["photos_per_day"]["gain"]["mean"] == pytest.approx(1 + 2.5 / 10)
    # 15時の記録は「昼」の2件目＝間食・追加の記録（夜食も晩の2件目）
    assert r["snacks_per_day"]["loss"]["mean"] == pytest.approx(1 + 2.5 / 20)
    assert r["snacks_per_day"]["gain"]["mean"] == pytest.approx(0.0)
    assert r["records_per_day"]["gain"]["mean"] == pytest.approx(2.0)
    assert r["photos_per_day"]["significant"] is True
    assert "成功群の方が" in r["photos_per_day"]["summary"]


def test_record_day_rate(planted):
    r = _by_key(m._record_behavior_compare())
    assert r["record_day_rate"]["loss"]["mean"] > r["record_day_rate"]["gain"]["mean"]


def test_late_night_counts_as_previous_evening():
    assert m._record_slot(1) == "night"
    assert m._record_slot(9) == "morning"
    assert m._record_slot(15) == "noon"


def test_report_shows_record_table():
    src = open(REPORT_SRC, encoding="utf-8").read()
    assert "記録のしかたの違い" in src


def test_admin_endpoint(planted):
    c = m.app.test_client()
    assert c.get("/api/admin/record-analysis").status_code in (401, 403, 404)
    r = c.get("/api/admin/record-analysis", headers={"X-Admin-Password": m.ADMIN_PASSWORD})
    d = r.get_json()
    assert r.status_code == 200 and d["loss_n"] == 6 and len(d["members"]) == 12


def test_report_payload_has_no_member_list(planted):
    """メール用のデータには会員ごとの内訳（名前）を入れない。"""
    rec = m._cut_success_compare()["record"]
    assert "members" not in rec


def test_dashboard_has_record_card():
    html = open(os.path.join(os.path.dirname(m.__file__), "templates", "admin.html"), encoding="utf-8").read()
    assert 'id="record-analysis"' in html and "loadRecordAnalysis()" in html


def test_untestable_metric_is_not_called_no_difference(planted):
    """ばらつきが無く検定できない項目を「差は見られません（p<0.001）」と書かないこと。"""
    r = _by_key(m._record_behavior_compare())["record_day_rate"]
    if r["p_holm"] is None and r["diff"] is not None:
        assert "検定はできません" in r["summary"]
        assert "差は見られません" not in r["summary"]
