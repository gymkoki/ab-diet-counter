# デイリーレポート／声かけの「対象範囲」の回帰テスト。
#
# オーナー方針 2026-09-09：
#   大幅に離脱した人を取り戻すのは難しいので、離脱者を呼び戻す施策はデイリーレポートから外す。
#   2週間以上まったく記録がない人は、デイリーレポートの対象者から外してよい。
#   いまは呼び戻しより「栄養計算の精度を上げること」の優先度が高い。
#
# ここで固定する不変条件：
#   ①コーチ提案・改善案・声かけ下書きに、14日以上記録がない会員を渡さない
#   ②プロンプトが「呼び戻し施策の提案」を明確に禁止している
#   ③管理画面の「長期離脱」一覧は人数把握用として残す（消さない）

import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
os.environ.setdefault("ADMIN_PASSWORD", "testpw")

import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _reset_db():
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        for t in ("coach_messages", "user_profile", "daily_b_count", "daily_weight"):
            cur.execute(f"DELETE FROM {t}")
        conn.commit()
    finally:
        conn.close()


def _seed(members):
    """members: [(uid, name, 最後に記録した日の「何日前」)]。None は一度も記録なし。
    プロフィールの updated_at（＝最後にアプリを使った日）も同じ日数だけさかのぼらせる。"""
    now = datetime.datetime.now(m.JST)
    today = now.date()
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        PH = m.PH
        for uid, name, last_days in members:
            used = now if last_days is None else now - datetime.timedelta(days=last_days)
            cur.execute(
                f"INSERT INTO user_profile (user_id, display_name, goal, updated_at) "
                f"VALUES ({PH},{PH},{PH},{PH})",
                (uid, name, "cut", used.isoformat()),
            )
            if last_days is None:
                continue
            cur.execute(
                f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) VALUES ({PH},{PH},{PH},{PH})",
                (uid, (today - datetime.timedelta(days=last_days)).isoformat(), 2.0, now.isoformat()),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_never_recorded(uid, name, used_days_ago):
    """Bカウントの記録が一度も無い会員（最後にアプリを使った日だけを持つ）。"""
    now = datetime.datetime.now(m.JST)
    used = now - datetime.timedelta(days=used_days_ago)
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO user_profile (user_id, display_name, goal, updated_at) "
            f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})",
            (uid, name, "cut", used.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(monkeypatch):
    m.init_db()
    m.app.config["TESTING"] = True
    monkeypatch.setattr(m, "get_client", lambda: object())
    today = datetime.datetime.now(m.JST).strftime("%Y-%m-%d")
    m._set_setting(f"coach-advice-{today}", "")
    m._set_setting(f"dev-proposals-{today}", "")
    with m.app.test_client() as c:
        yield c
    _reset_db()   # 他のテストへデータを残さない


# ── ① 離脱者の判定 ──────────────────────────────────────
def test_long_absent_is_14_days_or_more():
    assert m.REPORT_ABSENT_DAYS == 14
    assert m._is_long_absent({"days_since": 14}) is True
    assert m._is_long_absent({"days_since": 49}) is True
    assert m._is_long_absent({"days_since": 13}) is False
    assert m._is_long_absent({"days_since": 0}) is False
    # 記録が一度も無い会員は「最後にアプリを使った日」で判断する
    # （入会直後でこれから記録する人を、いきなり対象外にしないため）
    assert m._is_long_absent({"days_since": None, "days_since_app_use": 30}) is True
    assert m._is_long_absent({"days_since": None, "days_since_app_use": 2}) is False
    assert m._is_long_absent({"days_since": None, "days_since_app_use": None}) is False


def test_active_members_filters_long_absent():
    members = [
        {"uid": "a", "days_since": 0}, {"uid": "b", "days_since": 13},
        {"uid": "c", "days_since": 14}, {"uid": "d", "days_since": 44},
        {"uid": "e", "days_since": None, "days_since_app_use": 60},
        {"uid": "f", "days_since": None, "days_since_app_use": 0},   # 入会直後
    ]
    assert [x["uid"] for x in m._active_members(members)] == ["a", "b", "f"]


# ── ② コーチ提案（デイリーレポートの声かけリスト） ──────────────
def test_coach_advice_excludes_long_absent(client, monkeypatch):
    """49日・44日・14日 記録なしの会員は、AIへ渡すデータに含めないこと。"""
    _reset_db()
    _seed([("u-away49", "宮谷", 49), ("u-away14", "内野", 14),
           ("u-active", "現役さん", 1)])
    # 「一度も記録が無いが、今日アプリを使った」＝入会直後の人は対象に残す
    _seed_never_recorded("u-new", "入会直後さん", used_days_ago=0)
    # 「一度も記録が無く、アプリも1か月使っていない」＝離脱者として外す
    _seed_never_recorded("u-never", "未記録さん", used_days_ago=30)
    seen = {}

    def fake_ai(_c, user_text):
        seen["text"] = user_text
        return "■ 全体の状況\nテスト"

    monkeypatch.setattr(m, "_call_coach_ai", fake_ai)
    r = client.get("/api/admin/coach-advice", headers=ADMIN)
    assert r.status_code == 200
    text = seen["text"]
    assert "現役さん" in text and "入会直後さん" in text
    for gone in ("宮谷", "内野", "未記録さん"):
        assert gone not in text, f"離脱者({gone})がコーチ提案の対象に残っています"
    assert "減量希望メンバー：2名" in text


def test_coach_prompt_forbids_win_back_measures():
    p = m.COACH_PROMPT
    assert "直近14日以内に記録がある会員" in p
    assert "呼び戻" in p and "【禁止】" in p
    assert "復帰キャンペーン" in p
    # 「記録が途絶えた人を優先」という旧方針が残っていないこと
    assert "記録が途絶えた人・ペース未達の人を優先" not in p


# ── ③ 改善案（Claude Codeへの依頼文） ─────────────────────
def test_dev_proposal_prompt_prioritizes_nutrition_accuracy():
    p = m.DEV_PROPOSAL_PROMPT
    assert "栄養計算の精度" in p, "最優先テーマ（栄養計算の精度）が書かれていません"
    assert "復帰キャンペーン" in p and "【禁止】" in p


def test_dev_proposals_exclude_long_absent(client, monkeypatch):
    _reset_db()
    _seed([("u-away", "離脱さん", 30), ("u-active", "現役さん", 2)])
    seen = {}

    class _Block:
        type = "text"
        text = json.dumps({"proposals": [
            {"title": "t1", "why": "w", "effort": "小", "prompt": "p1"},
            {"title": "t2", "why": "w", "effort": "小", "prompt": "p2"},
            {"title": "t3", "why": "w", "effort": "小", "prompt": "p3"},
        ]}, ensure_ascii=False)

    class _Msg:
        content = [_Block()]

    class _Messages:
        def create(self, **kw):
            seen["text"] = kw["messages"][0]["content"][0]["text"]
            return _Msg()

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(m, "get_client", lambda: _Client())
    monkeypatch.setattr(m, "_collect_feature_usage", lambda: {})
    r = client.get("/api/admin/dev-proposals", headers=ADMIN)
    assert r.status_code == 200
    assert "現役さん" in seen["text"]
    assert "離脱さん" not in seen["text"], "離脱者が改善案の材料に残っています"


# ── ④ 個別の声かけ下書き ────────────────────────────────
def test_outreach_drafts_skip_long_absent():
    """2週間以上記録がない会員には、声かけ下書きを作らない。"""
    _reset_db()
    _seed([("u-away", "離脱さん", 30), ("u-slow", "3日空きさん", 3)])
    members = m._active_members(m._collect_cut_member_stats())
    picked = {p["uid"] for p in m._members_needing_outreach(members)}
    assert "u-slow" in picked
    assert "u-away" not in picked, "離脱者に自動で声かけ下書きが作られます"


# ── ⑤ 管理画面の「長期離脱」一覧は残す（人数把握用） ──────────────
def test_admin_dashboard_still_lists_long_absent(client):
    _reset_db()
    _seed([("u-away", "離脱さん", 30), ("u-active", "現役さん", 1)])
    r = client.get("/api/admin/attention-flags", headers=ADMIN)
    assert r.status_code == 200
    away = r.get_json()["long_absent"]
    assert [x["name"] for x in away] == ["離脱さん"]
    html = _read("templates/admin.html")
    assert "長期離脱" in html
    assert "再スタートのきっかけづくりを" not in html, "呼び戻しを促す文言が残っています"


# ── ⑥ レポート側の表記 ─────────────────────────────────
def test_report_states_the_scope():
    src = _read("report/send_report.py")
    assert "直近14日以内に記録がある会員" in src, "レポートに対象範囲の注記がありません"
    assert "日以上まったく記録がない会員は対象外です" in src


def test_no_loss_table_excludes_long_absent_members():
    """レポートの「痩せていない人の記録状況」表は、離脱者を外してから上位10名を選ぶこと。"""
    src = _read("app.py")
    i = src.index("no_loss_members = []")
    head = src[max(0, i - 900):i]
    assert "_recorded_within_slot_window" in head, "離脱者を除く処理がありません"
    assert "ranked = [t for t in ranked if _recorded_within_slot_window(t[1])]" in head
