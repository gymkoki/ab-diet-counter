"""体重の入力ミスの候補と削除の回帰テスト（オーナー指示 2026-11-17）。

指摘：「10月14日以降で1人だけ外れ値のような値がある。記入ミスだと思うので、この日付のこの外れ値の値だけ削除して」。
固定する不変条件：
  ① 候補は「その人の中央値から大きく外れた1日の値」だけ（ふだんの増減・記録が少ない人は出さない）
  ② 消せるのは管理者だけ・指定した1日の1件だけ・画面に出ていた値と同じときだけ
  ③ 消した値は、同じ日・同じ値で送り直されても復活しない（違う値なら受け付ける）
  ④ 履歴の取得で端末にも伝え、端末に残っている同じ値を消してもらう
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
U1, U2, U3 = "u-wo-typo", "u-wo-normal", "u-wo-few"


def _wipe():
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        for t in ("daily_weight", "weight_removed"):
            cur.execute(f"DELETE FROM {t} WHERE user_id IN ({m.PH},{m.PH},{m.PH})", (U1, U2, U3))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    m.init_db()
    _wipe()
    c = m.app.test_client()

    def w(uid, date, kg):
        assert c.post("/api/daily-weight", json={"user_id": uid, "date": date, "weight": kg}).status_code == 200
    # 入力ミス（60kg台の人の 35.2kg）
    for d, kg in (("2026-10-10", 60.4), ("2026-10-14", 35.2), ("2026-10-20", 60.1), ("2026-10-28", 59.6), ("2026-11-05", 59.2)):
        w(U1, d, kg)
    # ふだんの増減（2kg）は候補にしない
    for d, kg in (("2026-10-10", 72.0), ("2026-10-20", 70.5), ("2026-11-01", 70.0)):
        w(U2, d, kg)
    # 記録が2回だけの人は判断しない
    for d, kg in (("2026-10-10", 50.0), ("2026-10-20", 80.0)):
        w(U3, d, kg)
    yield c
    _wipe()


def _mine(items):
    return [x for x in items if x["user_id"] in (U1, U2, U3)]


# ── ① 候補 ──────────────────────────────────────────────
def test_only_the_typo_is_listed(client):
    r = client.get("/api/admin/weight-outliers", headers=ADMIN)
    assert r.status_code == 200
    items = _mine(r.get_json()["items"])
    assert [(x["user_id"], x["date"], x["weight"]) for x in items] == [(U1, "2026-10-14", 35.2)]
    o = items[0]
    assert o["median"] == 59.6 and o["prev"]["weight"] == 60.4 and o["next"]["weight"] == 60.1


# ── ② 削除 ──────────────────────────────────────────────
def test_delete_is_admin_only(client):
    r = client.post("/api/admin/weight-outliers/delete", json={"user_id": U1, "date": "2026-10-14", "weight": 35.2})
    assert r.status_code == 404
    assert client.get("/api/admin/weight-outliers").status_code == 404


def test_delete_removes_only_that_day(client):
    r = client.post("/api/admin/weight-outliers/delete", headers=ADMIN,
                    json={"user_id": U1, "date": "2026-10-14", "weight": 35.2})
    assert r.status_code == 200, r.get_json()
    d = client.get(f"/api/monthly-b?user_id={U1}").get_json()
    weights = {x["date"]: x["weight"] for x in d["daily"] if x["weight"] is not None}
    assert "2026-10-14" not in weights
    assert weights == {"2026-10-10": 60.4, "2026-10-20": 60.1, "2026-10-28": 59.6, "2026-11-05": 59.2}, "他の日を消した"
    assert d["weight_removed"] == [{"date": "2026-10-14", "weight": 35.2}]
    assert not _mine(client.get("/api/admin/weight-outliers", headers=ADMIN).get_json()["items"])


def test_delete_refuses_changed_or_missing_value(client):
    r = client.post("/api/admin/weight-outliers/delete", headers=ADMIN,
                    json={"user_id": U1, "date": "2026-10-14", "weight": 60.0})
    assert r.status_code == 409, "画面と違う値（本人が直した後など）は消さない"
    r = client.post("/api/admin/weight-outliers/delete", headers=ADMIN,
                    json={"user_id": U1, "date": "2026-10-15", "weight": 35.2})
    assert r.status_code == 404


# ── ③ 送り直しで復活しない ─────────────────────────────────────
def test_removed_value_is_not_revived_but_correction_is_accepted(client):
    client.post("/api/admin/weight-outliers/delete", headers=ADMIN,
                json={"user_id": U1, "date": "2026-10-14", "weight": 35.2})
    r = client.post("/api/daily-weight", json={"user_id": U1, "date": "2026-10-14", "weight": 35.2})
    assert r.status_code == 200 and r.get_json().get("ignored") == "removed"
    d = client.get(f"/api/monthly-b?user_id={U1}").get_json()
    assert all(x["date"] != "2026-10-14" or x["weight"] is None for x in d["daily"]), "同じ値が復活した"
    # 本人が正しい値を入れ直したら受け付ける
    client.post("/api/daily-weight", json={"user_id": U1, "date": "2026-10-14", "weight": 60.3})
    d = client.get(f"/api/monthly-b?user_id={U1}").get_json()
    assert {x["date"]: x["weight"] for x in d["daily"]}["2026-10-14"] == 60.3


def test_removed_table_is_backed_up():
    assert "weight_removed" in m._BACKUP_TABLES


# ── ④ 画面 ──────────────────────────────────────────────
def _read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


def test_member_app_drops_the_same_value_locally():
    html = _read("templates/index.html")
    body = html[html.index("async function syncHistoryFromServer()"):]
    body = body[:body.index("\n}\n")]
    assert "data.weight_removed" in body and "delete wlog[r.date]" in body
    assert "Math.abs(v - Number(r.weight)) < 0.05" in body, "本人が直した違う値まで消さない"


def test_admin_card_is_wired():
    html = _read("templates/admin.html")
    assert 'id="weight-outlier-card"' in html and "loadWeightOutliers()" in html
    fn = html[html.index("async function deleteWeightOutlier(i)"):]
    fn = fn[:fn.index("\n  }\n")]
    assert "confirm(" in fn and "元に戻せません" in fn, "確認なしで消している"
    assert re.search(r"loadAnalysisRanking\(\), loadWeightOutliers\(\)", html)
