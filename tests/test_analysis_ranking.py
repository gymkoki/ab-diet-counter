"""管理画面「🤖 AI解析回数ランキング」（オーナー指示 2026-11-11）の回帰テスト。

直近30日の会員別：解析回数・推定費用（1回4円）・食事記録として保存された件数と保存率。上位20人。
保存率が低い人が一目で分かること。
"""
import datetime
import os
import re
import shutil
import subprocess
import tempfile

import pytest

import app as m

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clean():
    m.init_db()
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("usage_log", "daily_nutrition", "action_log", "user_profile"):
        cur.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()


@pytest.fixture
def db():
    _clean()
    yield
    _clean()


def _day(n):
    return (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=n)).isoformat()


def _add(uid, analyses=0, meals_by_day=None, copies_by_day=None, name=None, days_ago=1):
    conn = m._get_conn()
    cur = conn.cursor()
    for _ in range(analyses):
        cur.execute(f"INSERT INTO usage_log (user_id, created_at) VALUES ({m.PH},{m.PH})",
                    (uid, f"{_day(days_ago)}T12:00:00+09:00"))
    for d, n in (meals_by_day or {}).items():
        cur.execute(f"INSERT INTO daily_nutrition (user_id,date,meal_count,protein_g,veg_g,created_at) "
                    f"VALUES ({m.PH},{m.PH},{m.PH},0,0,'x')", (uid, _day(d), n))
    for d, n in (copies_by_day or {}).items():
        for _ in range(n):
            cur.execute(f"INSERT INTO action_log (user_id,action,created_at) VALUES ({m.PH},'copy',{m.PH})",
                        (uid, f"{_day(d)}T08:00:00+09:00"))
    if name:
        cur.execute(f"INSERT INTO user_profile (user_id,display_name,updated_at) VALUES ({m.PH},{m.PH},'x')", (uid, name))
    conn.commit()
    conn.close()


def _rows():
    return {r["name"]: r for r in m._analysis_ranking()["rows"]}


def test_counts_cost_and_save_rate(db):
    # 10回解析して、残っている記録は2日で計8件（うち1件はコピーご飯）→ AIで残ったのは7件・70%
    _add("u-sato", analyses=10, meals_by_day={1: 5, 2: 3}, copies_by_day={2: 1}, name="佐藤")
    r = _rows()["佐藤"]
    assert r["analyses"] == 10 and r["cost_yen"] == 40
    assert r["saved"] == 7 and r["save_rate"] == 70 and r["level"] == "mid"


def test_low_save_rate_is_flagged(db):
    """解析しては消している人：12回解析して残っているのは3件 → 25%・赤。"""
    _add("u-kesu", analyses=12, meals_by_day={1: 3}, name="鈴木")
    r = _rows()["鈴木"]
    assert r["save_rate"] == 25 and r["level"] == "low"


def test_copy_meals_do_not_count_as_saved(db):
    """コピーご飯だけの日はAIを使っていないので保存件数に入れない（日ごとに0未満にはしない）。"""
    _add("u-copy", analyses=2, meals_by_day={1: 2, 3: 2}, copies_by_day={3: 5}, name="田中")
    r = _rows()["田中"]
    assert r["saved"] == 2 and r["save_rate"] == 100 and r["level"] == "ok"


def test_saved_never_exceeds_analyses(db):
    _add("u-x", analyses=2, meals_by_day={1: 6}, name="高橋")
    assert _rows()["高橋"]["saved"] == 2


def test_only_last_30_days(db):
    _add("u-old", analyses=5, meals_by_day={40: 5}, name="伊藤", days_ago=40)
    assert "伊藤" not in _rows()


def test_top20_sorted_by_analyses(db):
    for i in range(25):
        _add(f"u{i:02d}", analyses=i + 1, meals_by_day={1: i + 1})
    d = m._analysis_ranking()
    assert len(d["rows"]) == 20 and d["members"] == 25
    assert [r["analyses"] for r in d["rows"]] == list(range(25, 5, -1))
    assert d["total_analyses"] == sum(range(1, 26)) and d["total_cost_yen"] == d["total_analyses"] * 4


def test_admin_only_and_unnamed_fallback(db):
    _add("abcd1234", analyses=1, meals_by_day={1: 1})
    c = m.app.test_client()
    assert c.get("/api/admin/analysis-ranking").status_code in (401, 403, 404)
    d = c.get("/api/admin/analysis-ranking", headers=ADMIN).get_json()
    assert d["rows"][0]["name"] == "会員abcd"


def test_dashboard_card_and_js_syntax():
    html = open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8").read()
    assert 'id="analysis-ranking"' in html and "loadAnalysisRanking()" in html
    if shutil.which("node"):
        js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        js = re.sub(r"\{\{.*?\}\}", "null", js)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
        try:
            out = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
            assert out.returncode == 0, out.stderr
        finally:
            os.unlink(f.name)
