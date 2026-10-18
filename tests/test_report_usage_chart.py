# デイリーレポートの「日別 利用状況とAPI費用」グラフまわりの回帰テスト。
#
# 経緯：
#   ・2026-09-30 「日別 利用推移」と「API 日別コスト」の似たグラフ2枚を1枚（chart_usage）に統合した。
#   ・2026-10-18 その1枚もオーナー指示で削除した（「重複しているデータがあるので全部消して」）。
#     利用者数・解析回数・記録件数は「1. 利用統計」の数字、APIの費用は最上部のコストのカードに出ている。
#
# 守りたい不変条件：
#   ①日別の推移グラフ（chart_usage・chart_credit）をレポートに戻さない
#   ④（サーバー）記録件数・記録した人数の30日推移は、KPIと同じ数え方。明細が消えた過去日は
#     daily_nutrition → action_log の順で補う（最新日はKPIの数字と一致する）。
#     レポートのグラフは消したが、データはサーバーに残してある。

import datetime
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app as m  # noqa: E402


# ── ① 日別の推移グラフはレポートに載せない ─────────────────────────
def test_daily_usage_chart_stays_removed():
    with open(os.path.join(ROOT, "report", "send_report.py"), encoding="utf-8") as f:
        src = f.read()
    for name in ("chart_usage", "chart_credit"):
        assert f"def {name}(" not in src, f"{name} の関数が復活している（オーナー指示で削除）"
        assert f"cid:{name}" not in src, f"{name} がメール本文に復活している"
        assert f'"{name}":' not in src, f"{name} が添付画像に復活している"
    assert "<h2>📈 日別 利用状況とAPI費用" not in src, "見出しが残っている"
    assert "def _daily_cost_yen(" not in src, "グラフ専用だった日別費用の計算が残っている"


# ── ④ 記録件数・記録した人数の30日推移 ──────────────────────────
PREFIX = "rec-trend-"


def _exec(sql, params=()):
    with m._db_lock:
        conn = m._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


@pytest.fixture
def db():
    m.init_db()

    def clean():
        for t in ("daily_meals", "daily_nutrition", "action_log"):
            _exec(f"DELETE FROM {t} WHERE user_id LIKE {m.PH}", (PREFIX + "%",))
    clean()
    yield
    clean()


def _payload(n_items, empty=0):
    items = [{"id": i, "result": {"foods": [], "total_b_count": 1}} for i in range(n_items)]
    items += [{"id": 100 + i, "result": None} for i in range(empty)]   # 未解析は数えない
    return json.dumps({"food": {"items": items}})


def _meals(uid, date, n, empty=0):
    _exec(f"INSERT INTO daily_meals (user_id, date, payload, created_at) "
          f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, date, _payload(n, empty), date))


def _nutrition(uid, date, n):
    _exec(f"INSERT INTO daily_nutrition (user_id, date, meal_count, protein_g, veg_g, created_at) "
          f"VALUES ({m.PH},{m.PH},{m.PH},0,0,{m.PH})", (uid, date, n, date))


def _action(uid, date, action="photo"):
    _exec(f"INSERT INTO action_log (user_id, action, created_at) VALUES ({m.PH},{m.PH},{m.PH})",
          (uid, action, f"{date}T12:00:00+09:00"))


def test_record_trend_uses_each_source_in_order(db):
    # ほかのテストが実在の日付にデータを入れるため、誰も使わない2031年の日付で試す
    a, b = PREFIX + "a", PREFIX + "b"
    # 明細が残っている日（KPIと同じ元データ）
    _meals(a, "2031-02-28", 3, empty=2)
    _meals(b, "2031-02-28", 2)
    # 明細は消えたが要約が残っている日
    _nutrition(a, "2031-02-20", 4)
    _nutrition(b, "2031-02-20", 1)
    # 要約もまだ無い古い日：記録操作の履歴で補う（Bだけ追加・コピーご飯も数える）
    _action(a, "2031-02-05", "photo"); _action(a, "2031-02-05", "text")
    _action(b, "2031-02-05", "copy");  _action(b, "2031-02-05", "manual_b")

    counts, people = m._daily_record_trend("2031-02-01", "2031-02-28")
    assert counts["2031-02-28"] == 5 and people["2031-02-28"] == 2, "未解析の項目まで数えている"
    assert counts["2031-02-20"] == 5 and people["2031-02-20"] == 2
    assert counts["2031-02-05"] == 4 and people["2031-02-05"] == 2
    assert "2031-02-10" not in counts


def test_record_trend_prefers_detail_over_older_sources(db):
    """同じ日に複数の元データがあっても、明細（KPIと同じ数え方）を優先する。"""
    a = PREFIX + "a"
    _meals(a, "2031-02-28", 2)
    _nutrition(a, "2031-02-28", 9)
    for _ in range(7):
        _action(a, "2031-02-28")
    counts, people = m._daily_record_trend("2031-02-28", "2031-02-28")
    assert counts["2031-02-28"] == 2 and people["2031-02-28"] == 1


def test_record_trend_merges_older_sources_per_person(db):
    """明細が消えた日に、要約が一部の人の分しか無くても、残りの人を履歴で補うこと。
    （日ごとに丸ごと切り替えると、要約を始めた日などに大半の人が抜け落ちる）"""
    a, b = PREFIX + "a", PREFIX + "b"
    _nutrition(a, "2031-03-10", 4)                 # a は要約あり
    _action(a, "2031-03-10"); _action(a, "2031-03-10")   # a の履歴（要約を優先するので使わない）
    for _ in range(3):
        _action(b, "2031-03-10")                   # b は履歴だけ
    counts, people = m._daily_record_trend("2031-03-10", "2031-03-10")
    assert counts["2031-03-10"] == 7 and people["2031-03-10"] == 2


def test_record_trend_survives_db_failure(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "_get_conn", boom)
    assert m._daily_record_trend("2026-09-01", "2026-09-30") == ({}, {})


def test_report_data_last_day_matches_kpi(db):
    """レポートの最新日のグラフの値が、上のKPI（記録件数・記録した人数）と一致すること。"""
    target = (datetime.datetime.now(m.JST) - datetime.timedelta(days=1)).date().isoformat()
    _meals(PREFIX + "a", target, 3, empty=1)
    _meals(PREFIX + "b", target, 1)
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        r = c.get("/api/admin/report-data", headers={"X-Admin-Password": m.ADMIN_PASSWORD})
    assert r.status_code == 200
    d = r.get_json()
    assert d["dates"][-1] == target
    assert len(d["daily_records_trend"]) == len(d["dates"])
    assert len(d["daily_recorders_trend"]) == len(d["dates"])
    assert d["daily_records_trend"][-1] == d["meal_summary"]["count"]
    assert d["daily_recorders_trend"][-1] == d["meal_summary"]["users"]
