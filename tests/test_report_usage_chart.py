# デイリーレポートの「日別 利用状況とAPI費用」グラフの回帰テスト（オーナー指示 2026-09-30）。
#
# 経緯：「日別 利用推移」と「API 日別コスト」の似たグラフが毎回2枚出ていた。
#   1枚に統一し、次の5項目を必ず載せる：
#     デイリー利用者数／食事解析の回数／APIの費用／食事記録の件数／記録した人数
#
# 守りたい不変条件：
#   ①日別のグラフは1枚だけ（費用だけの別グラフを復活させない）
#   ②その1枚に5項目がすべて載っている
#   ③費用は、実額（Cost API）があれば実額、無ければ推定を使う
#   ④記録件数・記録した人数の30日推移は、KPIと同じ数え方。明細が消えた過去日は
#     daily_nutrition → action_log の順で補う（最新日はKPIの数字と一致する）

import datetime
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "report"))
import app as m  # noqa: E402

matplotlib = pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
import send_report  # noqa: E402

DATES = [f"2026-09-{d:02d}" for d in range(1, 31)]


def _data(**kw):
    d = {
        "dates": DATES,
        "daily_analyses_trend":  [100 + i for i in range(30)],
        "daily_users_trend":     [30 + i % 5 for i in range(30)],
        "daily_records_trend":   [110 + i for i in range(30)],
        "daily_recorders_trend": [32 + i % 5 for i in range(30)],
        "cost_per_analysis": 4.0,
    }
    d.update(kw)
    return d


def _capture_fig(monkeypatch):
    """chart_usage が描いた図をそのまま受け取る（凡例の中身を確かめるため）。"""
    box = {}

    def fake(fig):
        box["fig"] = fig
        return b"\x89PNG"
    monkeypatch.setattr(send_report, "fig_to_png", fake)
    return box


# ── ② 5項目が載っている ────────────────────────────────────────
def test_usage_chart_has_all_five_items(monkeypatch):
    box = _capture_fig(monkeypatch)
    send_report.chart_usage(_data())
    fig = box["fig"]
    labels = [t.get_text() for ax in fig.axes if ax.get_legend()
              for t in ax.get_legend().get_texts()]
    for need in ("デイリー利用者数", "食事解析の回数", "APIの費用", "食事記録の件数", "記録した人数"):
        assert any(need in lbl for lbl in labels), f"「{need}」がグラフに載っていない"
    title = fig.axes[0].get_title()
    assert "API費用" in title and "30日合計" in title


def test_usage_chart_renders_png():
    assert send_report.chart_usage(_data())[:4] == b"\x89PNG"


def test_usage_chart_tolerates_missing_record_series():
    """サーバーが古い（新しい項目を返さない）ときも落ちない。"""
    data = _data()
    del data["daily_records_trend"], data["daily_recorders_trend"]
    assert send_report.chart_usage(data)[:4] == b"\x89PNG"


# ── ③ 費用の出どころ ────────────────────────────────────────────
def test_cost_uses_real_cost_when_available():
    credit = {"status": "ok", "estimated": False, "usd_jpy": 150.0,
              "daily": {"2026-09-01": 2.0, "2026-09-02": 3.0}}
    yen, kind = send_report._daily_cost_yen(_data(), credit)
    assert kind == "実額"
    assert yen[0] == 300 and yen[1] == 450 and yen[2] == 0


def test_cost_marks_estimate():
    credit = {"status": "estimated", "estimated": True, "usd_jpy": 155.0,
              "daily": {"2026-09-01": 400 / 155.0}}
    yen, kind = send_report._daily_cost_yen(_data(), credit)
    assert kind == "推定" and yen[0] == 400


def test_cost_falls_back_to_analysis_count():
    """クレジット情報が取れないときは「解析回数×1回あたりの概算」で描く。"""
    yen, kind = send_report._daily_cost_yen(_data(), {"status": "error"})
    assert kind == "推定"
    assert yen[0] == 400 and yen[-1] == round(129 * 4)


# ── ① 日別グラフは1枚だけ ──────────────────────────────────────
def test_only_one_daily_chart_in_report():
    with open(os.path.join(ROOT, "report", "send_report.py"), encoding="utf-8") as f:
        src = f.read()
    assert "def chart_credit(" not in src, "費用だけの日別グラフが復活している（統合済み）"
    assert "cid:chart_credit" not in src
    assert 'charts["chart_credit"]' not in src
    assert "chart_usage(data, credit)" in src, "統合グラフに費用（credit）を渡していない"
    assert "日別 利用状況とAPI費用" in src


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
