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


def test_members_are_told_why_analysis_stops():
    """体重が無いと解析できない理由が、会員向けに画面で説明されていること。

    以前はこの件を告知した「最新アップデート No.31」を直接検査していたが、
    お知らせは新しい内容へ入れ替わっていくため、そこに固定すると
    お知らせを更新するたびにこのテストが落ちてしまう。
    検査対象を、常に出続ける案内モーダル（weight-gate-overlay）へ移した。
    """
    html = _html()
    m_ = re.search(r'id="weight-gate-overlay".*?</div>\s*</div>\s*</div>', html, re.S)
    assert m_, "体重入力の案内モーダルが見つかりません"
    block = m_.group(0)
    assert "1週間以上" in block, "何日で止まるのかが会員に伝わりません"
    assert "解析" in block and "体重" in block


# ── 体重の入力を先に・選んだ写真を消さない（オーナー指示 2026-11-10）────────────
# 指摘：「食事の写真をアップして解析ボタンを押すと『体重を記録しろ』と出て、さっきまで
# あげた食事の写真をまたもう一度やり直さないといけない。体重の記録がないときは、まず体重を
# 入力させてから次の画面に移れるようにして」。
def _fn(html, name):
    m_ = re.search(rf"(?:async )?function {name}\(.*?\n\}}", html, re.S)
    assert m_, f"{name} が無い"
    return m_.group(0)


def test_weight_is_asked_before_picking_photos_or_typing():
    html = _html()
    for fn, label in (("openFileInput", "写真を選ぶ"), ("chooseManualText", "入力へ進む")):
        body = _fn(html, fn)
        assert "hasWeightForGate()" in body and "openWeightGate({ after:" in body, \
            f"{fn}：体重が無いときに先に体重を入れてもらっていない"
        assert label in body
    # 写真の選択はタップの流れのまま開く必要がある（await を挟むとブラウザに止められる）
    save = _fn(html, "saveWeightFromGate")
    assert not save.startswith("async"), "saveWeightFromGate を async にすると写真の選択が開かなくなる"
    assert "after()" in save and "await" not in save
    assert "submitDailyWeight(" in save and "resumeWeightWaitingItems()" in save


def test_gate_modal_has_inline_weight_input():
    html = _html()
    block = re.search(r'id="weight-gate-overlay".*?</div>\s*</div>\s*</div>', html, re.S).group(0)
    assert 'id="weight-gate-input"' in block and "saveWeightFromGate()" in block
    assert 'id="weight-gate-save"' in block


def test_photos_are_kept_while_waiting_for_weight():
    html = _html()
    add = _fn(html, "addFiles")
    assert "if (!(await ensureWeightForAnalysis())) return;" not in add, "体重が無いと選んだ写真を捨てている"
    assert "waitingWeight: !weightOk" in add
    # サーバーが weight_required を返しても、写真・文章は消さずに体重待ちで残す
    for fn in ("analyzeItem", "resumeTextItem", "submitTextEntry"):
        body = _fn(html, fn)
        assert "weight_required" in body and "item.waitingWeight = true" in body, f"{fn}：体重待ちで残していない"
    # 体重を保存したら自動で解析を続ける・再読み込みしても体重待ちを覚えている
    resume = _fn(html, "resumeWeightWaitingItems")
    assert "analyzeItem(" in resume and "resumeTextItem(" in resume
    assert "waitingWeight: (!i.result && i.waitingWeight) || undefined" in _fn(html, "buildMealsPayload")
    assert "if (item.waitingWeight && !hasWeightForGate()) return;" in _fn(html, "resumePendingAnalyses")


def test_analysis_waits_for_weight_save():
    """保存した体重がサーバーに届く前に解析を送ると、また止められてしまう。"""
    html = _html()
    for fn in ("analyzeItem", "resumeTextItem", "submitTextEntry", "reanalyzeItem"):
        assert "await _awaitWeightSave();" in _fn(html, fn), f"{fn} が体重の送信を待っていない"


# ── Codex レビューの指摘（PR #258）────────────────────────────
def test_weight_save_wait_has_deadline():
    """体重の送信が応答しなくても、解析は期限つきで先へ進む（P1：有限時間で結論へ）。"""
    import shutil
    import subprocess
    import tempfile
    if shutil.which("node") is None:
        pytest.skip("node が見つからない")
    html = _html()
    fn = _fn(html, "_awaitWeightSave")
    assert "Promise.race" in fn and "WEIGHT_SAVE_WAIT_MS" in fn
    js = ("const WEIGHT_SAVE_WAIT_MS = 50;\nlet _weightSavePromise = new Promise(() => {});\n" + fn + """
const t0 = Date.now();
_awaitWeightSave().then(() => {
  console.log(JSON.stringify({ ms: Date.now() - t0, cleared: _weightSavePromise === null }));
});
""")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        path = f.name
    try:
        p = subprocess.run(["node", path], capture_output=True, text=True, timeout=10)
    finally:
        os.unlink(path)
    import json as _json
    out = _json.loads(p.stdout)
    assert out["ms"] < 1000 and out["cleared"], "応答しない体重の送信を待ち続けている"


def test_weight_screen_save_also_resumes_waiting_items():
    body = _fn(_html(), "saveTodayWeight")
    assert "_weightSavePromise = submitDailyWeight(" in body
    assert "resumeWeightWaitingItems()" in body, "体重/日記の画面で保存しても体重待ちが再開しない"


def test_resume_resets_deadline_and_counts_text_record():
    html = _html()
    assert "item.pickedAt = Date.now();" in _fn(html, "resumeWeightWaitingItems"), \
        "体重を入れていた時間が解析の持ち時間から引かれる"
    assert "logAction('text')" in _fn(html, "resumeTextItem"), "再開した文章の記録が集計に入らない"
