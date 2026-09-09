# 「1週間に一度は体重を入力しないと食事の解析ができない」の回帰テスト。
# オーナー指示 2026-09。ABダイエットは体重の変化を見ながら判定するため、
# 体重の記録が途切れている人には解析させず、体重入力へ誘導する。
#
# 守りたい不変条件：
#   ①体重が7日より古い（または一度も無い）人は /analyze・/analyze-text・/reanalyze が403
#   ②403の本文には weight_required が入る（画面側が案内モーダルを出す目印）
#   ③7日ちょうど前の記録は通す（毎週同じ曜日に測る人を弾かない）
#   ④体重を入れた瞬間から通る
#   ⑤DBが落ちているときは止めない（記録している人まで使えなくしない）
#   ⑥user_id が無いリクエストは止めない（体重を登録する手段が無く詰むため）

import io
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402
from conftest import clear_weight, seed_weight  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UID = "gate-user"


@pytest.fixture
def client(monkeypatch):
    m.init_db()
    m.app.config["TESTING"] = True
    monkeypatch.setattr(m, "get_client", lambda: object())
    monkeypatch.setattr(m, "create_and_parse", lambda *a, **k: {"foods": [], "total_b_count": 0})
    with m.app.test_client() as c:
        yield c


def _png():
    return (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "meal.png")


# ── 判定そのもの ────────────────────────────────────────────────
def test_has_recent_weight_boundaries():
    clear_weight(UID)
    assert m._has_recent_weight(UID) is False, "一度も記録が無ければ False"

    seed_weight(UID, days_ago=7)
    assert m._has_recent_weight(UID) is True, "7日ちょうど前は通す（毎週同じ曜日の人を弾かない）"

    clear_weight(UID)
    seed_weight(UID, days_ago=8)
    assert m._has_recent_weight(UID) is False, "8日前だけなら止める"

    seed_weight(UID, days_ago=0)
    assert m._has_recent_weight(UID) is True, "今日入れたら通る"


def test_db_failure_does_not_block(monkeypatch):
    """DBが落ちても解析は止めない（判定できないことで巻き添えにしない）。"""
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "_get_conn", _boom)
    assert m._has_recent_weight(UID) is True


def test_missing_user_id_is_not_blocked():
    """user_id が無いと体重を登録する手段が無いので、止めると詰む。"""
    assert m._weight_gate_blocked("") is None
    assert m._weight_gate_blocked(None) is None


# ── 各エンドポイント ────────────────────────────────────────────
def _assert_blocked(res):
    assert res.status_code == 403
    body = res.get_json()
    assert body["weight_required"] is True, "画面側が案内を出す目印が無い"
    assert "体重" in body["error"]


def test_analyze_is_blocked_without_weight(client):
    clear_weight(UID)
    res = client.post("/analyze", data={"image": _png(), "user_id": UID},
                      content_type="multipart/form-data")
    _assert_blocked(res)


def test_analyze_text_is_blocked_without_weight(client):
    clear_weight(UID)
    res = client.post("/analyze-text", data={"text": "おにぎり2個", "user_id": UID})
    _assert_blocked(res)


def test_reanalyze_is_blocked_without_weight(client):
    clear_weight(UID)
    res = client.post("/reanalyze", data={"correction": "白米は75gです", "user_id": UID})
    _assert_blocked(res)


def test_analysis_works_again_after_recording_weight(client):
    """体重を入れた瞬間から通る（これが会員の復旧手段）。"""
    clear_weight(UID)
    assert client.post("/analyze-text", data={"text": "おにぎり", "user_id": UID}).status_code == 403

    ok = client.post("/api/daily-weight", json={
        "user_id": UID, "weight": 70.5,
        "date": __import__("datetime").datetime.now(m.JST).date().isoformat()})
    assert ok.status_code == 200

    res = client.post("/analyze-text", data={"text": "おにぎり", "user_id": UID})
    assert res.status_code == 200, "体重を入れたのに解析できていない"


def test_blocked_request_is_not_counted_as_usage(client):
    """止めた分はAIを呼んでいないので、利用回数（＝費用）に数えない。"""
    calls = []
    m.init_db()
    clear_weight(UID)
    import app
    orig = app._log_usage
    app._log_usage = lambda uid: calls.append(uid)
    try:
        client.post("/analyze-text", data={"text": "おにぎり", "user_id": UID})
    finally:
        app._log_usage = orig
    assert calls == [], "解析していないのに利用回数が増えている"


# ── 画面側 ──────────────────────────────────────────────────────
def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def test_frontend_gate_exists():
    html = _html()
    # 案内モーダルと、体重入力へ飛ぶボタン
    assert 'id="weight-gate-overlay"' in html
    assert "openWeightRecord()" in html
    for fn in ("function hasWeightForGate", "async function ensureWeightForAnalysis",
               "function openWeightGate", "function _handledWeightGate"):
        assert fn in html, f"{fn} が無い"


def test_frontend_blocks_every_analysis_entry_point():
    """写真・文章・再計算のどれからでも、解析前に体重を確認すること。"""
    html = _html()
    for fn in ("addFiles", "submitTextEntry", "reanalyzeItem"):
        body = re.search(rf"async function {fn}\(.*?\n\}}", html, re.S).group(0)
        assert "ensureWeightForAnalysis" in body, f"{fn} が体重を確認していない"


def test_frontend_window_matches_server():
    """画面側の判定窓がサーバーとずれていないこと（ずれると挙動が食い違う）。"""
    html = _html()
    assert f"const WEIGHT_GATE_MAX_AGE_DAYS = {m.WEIGHT_GATE_MAX_AGE_DAYS};" in html
    # 「今日から7日前まで」＝今日を含めて8日ぶんを見る
    assert "hasRecentWeight(WEIGHT_GATE_MAX_AGE_DAYS + 1)" in html


def test_update_notice_tells_members():
    """この件はオーナー指示でお知らせに出す（No.とNOTICE_KEYを更新済みか）。"""
    html = _html()
    assert "最新アップデート No.31" in html
    assert "ab-diet-notice-v33" in html
    assert "週に1回は体重の記録" in html
