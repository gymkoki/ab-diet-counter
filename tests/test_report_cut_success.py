"""デイリーレポート④「減量希望者：減量成功群 vs 失敗群」の回帰テスト。

オーナー指示 2026-10-01：
  見たいのは「減量希望 vs 体重維持」ではなく、減量希望者の中で
  減量に成功した人と失敗した人の、カロリー・野菜・タンパク質の違い。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
import app as m  # noqa: E402
from test_diet_analysis import seeded  # noqa: E402,F401  合成データ（減量6人・増量6人）

import pytest  # noqa: E402


def _set_goal(uids, goal):
    conn = m._get_conn()
    cur = conn.cursor()
    for u in uids:
        cur.execute(f"INSERT INTO user_profile (user_id, goal, updated_at) VALUES ({m.PH},{m.PH},'x')",
                    (u, goal))
    conn.commit()
    conn.close()


def test_only_cut_members_are_compared(seeded):  # noqa: F811
    """目標が「減量」の会員だけで成功群／失敗群を作ること（維持の会員は混ぜない）。"""
    _set_goal(["loss-0", "loss-1", "loss-2", "gain-0", "gain-1"], "cut")
    _set_goal(["loss-3", "gain-2"], "maintain")
    csc = m._cut_success_compare()
    assert csc["params"]["goal"] == "cut"
    assert csc["coverage"]["loss_n"] == 3
    assert csc["coverage"]["gain_n"] == 2
    keys = {r["key"] for r in csc["results"]}
    assert {"kcal", "protein_g", "veg_g"} <= keys
    assert "members" not in csc, "会員ごとの明細をレポートに出さないこと"


def test_not_losing_counts_as_failure(seeded):  # noqa: F811
    """減量希望者で体重が減っていない人（傾き0含む）は失敗群に入ること。"""
    r = m._diet_analysis(threshold=5.0, goal="cut", flat_as_gain=True)
    assert r["coverage"]["flat_excluded"] == 0


def test_report_shows_success_vs_failure():
    # レポート用ライブラリ（requests・matplotlib）が無い環境（CI）では飛ばす
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report as sr
    src = open(sr.__file__, encoding="utf-8").read()
    assert "減量成功群 vs 失敗群" in src
    assert "減量希望 vs 体重維持" not in src, "旧い比較（減量希望 vs 体重維持）が戻っています"
    data = {"cut_success_compare": {
        "coverage": {"loss_n": 4, "gain_n": 3},
        "results": [{"key": "kcal", "loss": {"mean": 1650}, "gain": {"mean": 1980}, "diff": -330},
                    {"key": "protein_g", "loss": {"mean": 80}, "gain": {"mean": 62}, "diff": 18},
                    {"key": "veg_g", "loss": {"mean": 340}, "gain": {"mean": 210}, "diff": 130,
                     "significant": True}]}}
    assert sr.chart_goal_compare(data)[:4] == b"\x89PNG"
    assert sr.chart_goal_compare({})[:4] == b"\x89PNG", "データが無くても落ちないこと"


def test_b_count_average_is_compared(seeded):  # noqa: F811
    """オーナー要望 2026-10-07「Bの平均数も知りたい」：
    サーバーは成功群・失敗群それぞれの1日平均Bカウントを返すこと。"""
    _set_goal(["loss-0", "loss-1", "gain-0", "gain-1"], "cut")
    csc = m._cut_success_compare()
    b = next(r for r in csc["results"] if r["key"] == "b_count")
    assert b["loss"]["mean"] is not None and b["gain"]["mean"] is not None


def test_report_table_and_chart_include_b_count(seeded):  # noqa: F811
    """実際のレポート用データ（/api/admin/report-data）からメール本文を作り、
    ④の表と図に成功群・失敗群のBの平均が出ること。"""
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report as sr
    assert ("b_count", "Bカウント", "回", 1) in sr.CUT_COMPARE_METRICS
    _set_goal(["loss-0", "loss-1", "gain-0", "gain-1"], "cut")
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        data = c.get("/api/admin/report-data",
                     headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()
    b = sr._cut_metric(data["cut_success_compare"], "b_count")
    ok, ng = b["loss"]["mean"], b["gain"]["mean"]
    html = sr.build_html(data)
    sec = html[html.index("減量成功群 vs 失敗群"):]
    assert f"{ok:,.1f}回" in sec and f"{ng:,.1f}回" in sec, "④の表にBの平均が出ていない"
    assert sr.chart_goal_compare(data)[:4] == b"\x89PNG"
