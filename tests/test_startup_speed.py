"""アプリの起動が遅い問題（白い画面／「接続に時間がかかっています」）の回帰テスト。

経緯（2026-08）：
  オーナーから「有料プランなのに開くと10秒白い画面が出て、
  『サーバー起動中… 初回のみ30秒ほどかかります』と表示される」と報告。

  原因は Render のプランではなく gunicorn の起動設定だった。
  --preload が無いまま --workers 1 --max-requests 300 だったため、
  約300リクエストごとにワーカーが再起動し、そのたびに app.py の import と
  init_db() をやり直していた（実測5秒以上、本番のPostgreSQL環境ではさらに長い）。
  その間アプリは一切応答できず、利用者には白い画面として見えていた。

対策：
  ① gunicorn に --preload を付け、再起動をforkだけで済ませる
  ② --max-requests を 300 → 2000 に緩和し、再起動の頻度自体を下げる
  ③ サービスワーカーで直前の画面を即座に出し、白い画面を無くす
  ④ 「初回のみ30秒」という無料プラン時代の文言を実態に合わせて修正
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


# ── ① gunicorn の起動設定 ──────────────────────────────────────
def test_gunicorn_uses_preload():
    """--preload が無いと、ワーカー再起動のたびに数十秒アプリが止まる。"""
    render = _read("render.yaml")
    start = next(l for l in render.splitlines() if "gunicorn" in l)
    assert "--preload" in start, "gunicorn に --preload が付いていません（再起動のたびに無応答になります）"


def test_max_requests_is_not_too_aggressive():
    """再起動が頻繁すぎると体感速度を大きく損なう。"""
    render = _read("render.yaml")
    m = re.search(r"--max-requests\s+(\d+)", render)
    assert m, "--max-requests の指定が見当たりません"
    assert int(m.group(1)) >= 1000, \
        f"--max-requests が {m.group(1)} 回と頻繁すぎます（再起動が多発します）"


# ── ③ サービスワーカー ──────────────────────────────────────────
def test_service_worker_is_served_from_root():
    """/static/sw.js のままではトップページを制御できない（スコープの制約）。"""
    src = _read("app.py")
    assert '@app.route("/sw.js")' in src, "/sw.js がルート直下で配信されていません"
    assert "Service-Worker-Allowed" in src


def test_service_worker_never_caches_api():
    """記録データ(/api/)をキャッシュすると、古いBカウントや体重が表示されて事故になる。"""
    sw = _read("static", "sw.js")
    assert "'/api/'" in sw or '"/api/"' in sw
    assert "startsWith('/api/')" in sw, "/api/ を除外する処理が見当たりません"


def test_service_worker_is_network_first():
    """キャッシュ優先だと修正がすぐ反映されない。ネットワーク優先＋短いタイムアウトにする。"""
    sw = _read("static", "sw.js")
    m = re.search(r"NETWORK_TIMEOUT_MS\s*=\s*(\d+)", sw)
    assert m, "ネットワークのタイムアウト設定が見当たりません"
    assert 500 <= int(m.group(1)) <= 3000, "タイムアウトが極端です（500〜3000msが妥当）"
    # 成功したレスポンスだけ保存する（500やエラー画面を保存しない）
    assert "res.ok" in sw


def test_service_worker_is_registered():
    html = _read("templates", "index.html")
    assert "navigator.serviceWorker.register('/sw.js')" in html


# ── ④ 無料プラン時代の文言が残っていないこと ────────────────────
def test_no_stale_free_plan_wording():
    """有料プランではスリープしないので「初回のみ30秒」は誤った案内になる。

    判定対象は会員に見えるバナー本文だけ（コード内の経緯コメントは対象外）。
    """
    html = _read("templates", "index.html")
    m = re.search(r'<div id="warmup-banner">(.*?)</div>\s*</div>', html, re.S)
    assert m, "warmup-banner が見つかりません"
    banner_text = m.group(1)
    assert "初回のみ30秒" not in banner_text, "無料プラン時代の文言が残っています"
    assert "サーバー起動中" not in banner_text, "無料プラン時代の文言が残っています"


# ── 再起動を検知できること ──────────────────────────────────────
@pytest.fixture
def client():
    import app as m
    m.init_db()
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c


def test_status_exposes_uptime(client):
    """uptime が短いままなら再起動が繰り返されている、と判断できるようにする。"""
    d = client.get("/api/status").get_json()
    assert "uptime_sec" in d and isinstance(d["uptime_sec"], int)
    assert d["uptime_sec"] >= 0


def test_server_health_requires_admin(client):
    import app as m
    assert client.get("/api/admin/server-health").status_code == 404
    d = client.get("/api/admin/server-health",
                   headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()
    assert "uptime_sec" in d and "verdict" in d
    assert d["db"] in ("PostgreSQL", "SQLite")


def test_sw_js_is_reachable(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers.get("Service-Worker-Allowed") == "/"
    # SW自体が長期キャッシュされると、停止したくても止められなくなる
    assert "no-cache" in (r.headers.get("Cache-Control") or "")
