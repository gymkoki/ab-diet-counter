# 管理画面「🔎 会員を名前で検索（通算の体重の変化）」の回帰テスト（オーナー指示 2026-10-25）。
#
# 「名前で検索したら、その人が通算で何キロやせたかが分かるようにして」。
# 管理画面の会員一覧は「直近3日に記録した会員」しか出ないため、ブラウザの検索（Ctrl+F）では
# 見つからない人がいた。全会員（/api/admin/users）から名前で探し、
#   通算＝最初に記録した体重 → いちばん新しい体重 の変化（kg・%）・期間・記録回数 を出す。
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")


def _admin_html():
    with open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def db():
    m.init_db()

    def clean():
        conn = m._get_conn()
        cur = conn.cursor()
        for t in ("user_profile", "daily_weight", "daily_b_count"):
            cur.execute(f"DELETE FROM {t} WHERE user_id LIKE 'ms-%'")
        conn.commit()
        conn.close()
    clean()
    m.app.config["TESTING"] = True
    yield
    clean()


def _seed(uid, name, weights):
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO user_profile (user_id, display_name, goal, updated_at) VALUES ({m.PH},{m.PH},'cut','x')",
                (uid, name))
    for d, w in weights:
        cur.execute(f"INSERT INTO daily_weight (user_id,date,weight,created_at) VALUES ({m.PH},{m.PH},{m.PH},'x')",
                    (uid, d, w))
    conn.commit()
    conn.close()


def test_users_api_has_whole_period_weights(db):
    _seed("ms-1", "松澤", [("2026-08-07", 70.0), ("2026-09-15", 68.2), ("2026-10-24", 66.5)])
    with m.app.test_client() as c:
        users = c.get("/api/admin/users", headers=ADMIN).get_json()["users"]
    u = next(x for x in users if x["user_id"] == "ms-1")
    assert u["initial_weight"] == 70.0 and u["latest_weight"] == 66.5
    assert u["weight_change"] == -3.5
    assert u["weight_start_date"] == "2026-08-07" and u["weight_latest_date"] == "2026-10-24"
    assert u["weight_records"] == 3


def test_users_api_is_admin_only(db):
    with m.app.test_client() as c:
        assert c.get("/api/admin/users").status_code == 404


def test_search_card_is_on_the_dashboard():
    html = _admin_html()
    assert 'id="member-search-input"' in html and "oninput=\"memberSearch()\"" in html
    body = re.search(r"async function memberSearch\(\) \{.*?\n  \}", html, re.S).group(0)
    assert "fetch('/api/admin/users')" in body, "全会員から探すこと（直近の会員一覧だけでは見つからない人がいる）"
    # 検索カードは会員一覧より上（開いてすぐ使える場所）
    assert html.index('id="member-search-card"') < html.index("📧 デイリーレポートのメール")


def _run_js(code):
    html = _admin_html()
    fns = re.search(r"  let _msUsers = null.*?\n  async function memberSearch", html, re.S).group(0)
    fns = fns.rsplit("\n  async function memberSearch", 1)[0]
    esc = re.search(r"  function escapeHtml\(s\) \{.*?\n  \}", html, re.S).group(0)
    harness = f"const ADMIN_BASE = '/reall-kanri';\n{esc}\n{fns}\n{code}"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(harness)
        path = tmp.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout
    finally:
        os.unlink(path)


@needs_node
def test_name_matching_ignores_width_and_kana():
    out = json.loads(_run_js("""console.log(JSON.stringify([
      _msNorm('松澤 花子').includes(_msNorm('松澤')),
      _msNorm('マツザワ').includes(_msNorm('まつざわ')),
      _msNorm('ＭＡＴＳＵ').includes(_msNorm('matsu')),
    ]));"""))
    assert out == [True, True, True]


@needs_node
def test_result_shows_total_loss():
    u = {"user_id": "ms-1", "user_id_short": "ms-1…", "display_name": "松澤", "goal": "cut", "goal_label": "減量希望",
         "initial_weight": 70.0, "latest_weight": 66.5, "weight_start_date": "2026-08-07",
         "weight_latest_date": "2026-10-24", "weight_records": 3}
    html = _run_js(f"console.log(memberSearchResultHtml({json.dumps(u, ensure_ascii=False)}));")
    assert "通算 −3.5 kg やせました" in html
    assert "−5.0%" in html
    assert "開始 <b>70.0 kg</b>（2026/8/7）" in html and "現在 <b>66.5 kg</b>（2026/10/24）" in html
    assert "78日間" in html and "体重の記録 3回" in html
    assert "/reall-kanri/user/ms-1" in html


@needs_node
def test_result_for_gain_and_missing_weights():
    gain = {"user_id": "a", "display_name": "増", "initial_weight": 60, "latest_weight": 61.2,
            "weight_start_date": "2026-10-01", "weight_latest_date": "2026-10-20", "weight_records": 2}
    one = {"user_id": "b", "display_name": "一回", "initial_weight": 60, "latest_weight": 60,
           "weight_start_date": "2026-10-01", "weight_latest_date": "2026-10-01", "weight_records": 1}
    none = {"user_id": "c", "display_name": "<b>なし</b>"}
    out = _run_js("\n".join(f"console.log(memberSearchResultHtml({json.dumps(x, ensure_ascii=False)}));"
                            for x in (gain, one, none)))
    assert "通算 +1.2 kg 増えました" in out
    assert "体重の記録が1回だけ" in out
    assert "体重の記録がまだありません" in out
    assert "<b>なし</b>" not in out, "名前はエスケープする"


@needs_node
def test_admin_scripts_have_no_syntax_errors():
    html = _admin_html()
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    joined = re.sub(r"\{\{.*?\}\}", "''", "\n;\n".join(scripts))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(joined)
        path = tmp.name
    try:
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    finally:
        os.unlink(path)
