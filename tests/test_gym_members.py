"""リオールジム会員は無料（オーナー指示 2026-11-28）の回帰テスト。

「コードにしつつ、ニックネームと本名を登録させて、本名を見てリオールジムの会員ではない人ははじく」。
ここで固定する約束：
  ①受付前（管理画面でオフ・有料化前）は登録できず、会員の画面にも出ない
  ②正しいコード＋本名＋ニックネームで登録すると、有料化後も無料（billing_state が "gym"・解析が止まらない）
  ③コード違いは回数制限つきで断る。本名・ニックネームが空なら断る
  ④通行証のある記録は、その持ち主の端末からしか登録できない
  ⑤オーナーが「外す」と有料の対象に戻り、同じ記録では登録し直せない。「戻す」で無料に戻る
  ⑥有料化後（合言葉が終わってアカウントで守る時期）も、ジム会員はアカウント無しで解析できる
  ⑦本名は管理画面の一覧にだけ出る（表示名はニックネーム）。管理APIは管理者だけ
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
os.environ.setdefault("ADMIN_PASSWORD", "testpw")

import app as m  # noqa: E402
from conftest import unlock_device  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reset():
    m.init_db()
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        for t in ("gym_members", "billing_accounts", "member_devices", "accounts", "daily_b_count", "user_profile"):
            cur.execute(f"DELETE FROM {t}")
        cur.execute("DELETE FROM settings WHERE key IN ('gym_member_code','gym_signup_open','billing_disabled')")
        conn.commit()
    finally:
        conn.close()
    m._unlock_fails.clear()


@pytest.fixture
def opened():
    """受付中（管理画面でオン）。有料化はまだ。"""
    _reset()
    m._set_setting(m.GYM_SIGNUP_OPEN_KEY, "1")
    yield
    _reset()


@pytest.fixture
def billing_on(monkeypatch):
    """有料化が始まった状態（Stripe の鍵あり・開始日を過ぎている・合言葉の時期も過ぎている）。"""
    _reset()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(m, "BILLING_START", "2020-01-01")
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    yield
    _reset()


def _register(c, uid="u-gym", code=None, real="山田 花子", nick="はなこ", token=None):
    headers = {"X-Device-Token": token} if token else {}
    return c.post("/api/gym/register", headers=headers,
                  json={"user_id": uid, "code": code or m._gym_code(), "real_name": real, "nickname": nick})


def _record(uid, date):
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) VALUES ({m.PH},{m.PH},{m.PH},{m.PH})",
                    (uid, date.isoformat(), 3.0, date.isoformat()))
        conn.commit()
    finally:
        conn.close()


# ── ① 受付前 ─────────────────────────────────────────────
def test_closed_before_owner_opens_it():
    _reset()
    with m.app.test_client() as c:
        r = _register(c)
        st = c.get("/api/billing/status?user_id=u-gym").get_json()
        ms = c.get("/api/member-status?user_id=u-gym").get_json()
    assert r.status_code == 403
    assert st["gym"]["open"] is False and st["gym"]["member"] is False
    assert ms["gym_signup"] is False


def test_default_code_is_current_passphrase_and_can_be_changed():
    _reset()
    assert m._gym_code() == m._member_password()
    with m.app.test_client() as c:
        assert c.post("/api/admin/gym-members/settings", json={"code": "4321"}, headers=ADMIN).status_code == 200
        assert c.post("/api/admin/gym-members/settings", json={"code": "1"}, headers=ADMIN).status_code == 400
    assert m._gym_code() == "4321"
    _reset()


# ── ② 登録すると無料 ───────────────────────────────────────
def test_register_makes_member_free_after_billing_starts(billing_on):
    _record("u-gym", datetime.date(2019, 12, 1))          # 開始前から使っている会員（無料期間なし）
    tok = unlock_device("u-gym", "tok-gym")
    assert m.billing_state("u-gym")["state"] == "required"
    with m.app.test_client() as c:
        r = _register(c, token=tok)
        assert r.status_code == 200 and r.get_json()["token"] == tok   # 前の端末を締め出さない
        st = c.get("/api/billing/status?user_id=u-gym").get_json()
    assert st["state"] == "gym" and st["required"] is False and st["gym"]["member"] is True
    assert m._billing_blocked("u-gym") is None
    # 表示名はニックネーム（本名ではない）
    assert m._gym_row("u-gym")["real_name"] == "山田 花子"
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT display_name FROM user_profile WHERE user_id={m.PH}", ("u-gym",))
        assert cur.fetchone()[0] == "はなこ"
    finally:
        conn.close()


def test_gym_member_can_analyze_without_account_after_billing_starts(billing_on, monkeypatch):
    """⑥ 有料化後は合言葉が終わりアカウントで守るが、ジム会員はアカウント無しで解析できる。"""
    called = {"n": 0}
    monkeypatch.setattr(m, "get_client", lambda: called.__setitem__("n", called["n"] + 1) or None)
    with m.app.test_client() as c:
        r = c.post("/analyze-text", data={"text": "カレー", "user_id": "u-new"})
        assert r.status_code == 403 and r.get_json().get("account_required")      # 登録前は止まる
        tok = _register(c, uid="u-new").get_json()["token"]
        r = c.post("/analyze-text", data={"text": "カレー", "user_id": "u-new"}, headers={"X-Device-Token": tok})
        ms = c.get(f"/api/member-status?user_id=u-new&token={tok}").get_json()
    assert r.status_code not in (402, 403) and called["n"] == 1
    assert ms["account_required"] is False and ms["gym_signup"] is True


# ── ③ 入力の確認 ─────────────────────────────────────────
def test_wrong_code_is_rejected_and_rate_limited(opened):
    with m.app.test_client() as c:
        for _ in range(m.MEMBER_UNLOCK_MAX_FAILS):
            assert _register(c, code="000000").status_code == 401
        assert _register(c).status_code == 429                 # 正しいコードでもロック中は断る
    assert m._gym_row("u-gym") is None


@pytest.mark.parametrize("real,nick", [("", "はなこ"), ("山田 花子", ""), ("   ", "  ")])
def test_names_are_required(opened, real, nick):
    with m.app.test_client() as c:
        assert _register(c, real=real, nick=nick).status_code == 400


# ── ④ 他人の記録を登録できない ───────────────────────────────
def test_only_owner_device_can_register_existing_record(opened):
    unlock_device("u-gym", "owner-token")
    with m.app.test_client() as c:
        assert _register(c).status_code == 403                       # 通行証なし
        assert _register(c, token="someone-else").status_code == 403  # 違う通行証
        assert _register(c, token="owner-token").status_code == 200
    assert m._is_gym_member("u-gym")


# ── ⑤ 外す・戻す ────────────────────────────────────────
def test_owner_removes_non_member(billing_on):
    _record("u-x", datetime.date(2019, 12, 1))
    with m.app.test_client() as c:
        tok = _register(c, uid="u-x", real="知らない 人").get_json()["token"]
        assert m.billing_state("u-x")["state"] == "gym"
        assert c.post("/api/admin/gym-members/u-x", json={"status": "removed"}, headers=ADMIN).status_code == 200
        assert m.billing_state("u-x")["state"] == "required"
        r = _register(c, uid="u-x", token=tok)                       # 同じ記録では登録し直せない
        assert r.status_code == 403 and r.get_json()["removed"] is True
        st = c.get("/api/billing/status?user_id=u-x").get_json()
        assert st["gym"]["removed"] is True and st["gym"]["member"] is False
        assert c.post("/api/admin/gym-members/u-x", json={"status": "active"}, headers=ADMIN).status_code == 200
    assert m.billing_state("u-x")["state"] == "gym"


def test_db_error_does_not_crash_status(billing_on, monkeypatch):
    def boom(uid):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "_gym_row", boom)
    assert m._gym_info_safe("u-any") == {"open": False, "member": False, "removed": False}
    # 照会に失敗しても、非会員と取り違えて解析を止めない（Codex 指摘 2026-11-28）
    _record("u-any", datetime.date(2019, 12, 1))
    assert m._billing_blocked("u-any") is None
    with m.app.test_client() as c:
        assert c.get("/api/billing/status?user_id=u-any").get_json()["required"] is False


# ── ⑦ 管理画面 ─────────────────────────────────────────
def test_admin_list_shows_real_name_and_requires_admin(opened):
    with m.app.test_client() as c:
        _register(c, uid="u-a", real="佐藤 一郎", nick="いちろう")
        assert c.get("/api/admin/gym-members").status_code in (401, 404)
        d = c.get("/api/admin/gym-members", headers=ADMIN).get_json()
    assert d["active"] == 1 and d["signup_open"] is True
    assert d["members"][0]["real_name"] == "佐藤 一郎" and d["members"][0]["nickname"] == "いちろう"


def test_real_name_not_in_member_facing_status(opened):
    with m.app.test_client() as c:
        _register(c, real="鈴木 太郎", nick="たろう")
        d = c.get("/api/billing/status?user_id=u-gym").get_json()
    # 持ち主を確かめない公開の状況APIには、本名もニックネームも入れない（Codex 指摘 2026-11-28）
    body = __import__("json").dumps(d, ensure_ascii=False)
    assert "鈴木" not in body and "たろう" not in body and d["gym"]["member"] is True


def test_gym_members_are_backed_up():
    assert "gym_members" in m._BACKUP_TABLES


def test_ui_and_policy_texts():
    html = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
    admin = open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8").read()
    legal = open(os.path.join(ROOT, "templates", "legal.html"), encoding="utf-8").read()
    assert 'id="gym-card" style="display:none"' in html          # 受付が始まるまで出さない
    assert "submitGymRegister" in html and "/api/gym/register" in html
    assert "loadGymMembers()" in admin and "/api/admin/gym-members" in admin
    assert "本名" in legal and "リオールジムの会員であることを確かめるため" in legal


# ── Codex の指摘（2026-11-28）への対応 ────────────────────────
def test_preview_flag_does_not_bypass_closed_signup(monkeypatch):
    """受付オフのまま、クライアントが preview を付けても登録できない。"""
    _reset()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(m, "BILLING_START", "2099-01-01")
    with m.app.test_client() as c:
        r = c.post("/api/gym/register", json={"user_id": "u-p", "code": m._gym_code(), "real_name": "a b",
                                              "nickname": "c", "preview": True})
        st = c.get("/api/billing/status?user_id=u-p&preview=1").get_json()
    assert r.status_code == 403 and m._gym_row("u-p") is None
    assert st["gym"]["open"] is False
    _reset()


def test_existing_subscription_is_still_shown_for_gym_member(billing_on):
    """先に月額プランへ申し込んだ人がジム会員になったら、契約が続いていることを画面に出す（請求が続くため）。"""
    _record("u-sub", datetime.date(2019, 12, 1))
    m._upsert_billing("u-sub", customer_id="cus_1", subscription_id="sub_1", status="active",
                      current_period_end="2026-12-20T00:00:00+09:00", cancel_at_period_end=0)
    with m.app.test_client() as c:
        _register(c, uid="u-sub")
        st = c.get("/api/billing/status?user_id=u-sub").get_json()
    assert st["state"] == "gym" and st["gym_subscription"] is True and st["can_manage"] is True
    assert st["current_period_end"].startswith("2026-12-20")
    html = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
    assert "月額プランのお支払いが続いています" in html


@pytest.mark.parametrize("saved,typed", [("１２３", "123"), ("860", "８６０"), ("あいう", "あいう")])
def test_code_comparison_handles_fullwidth_and_non_ascii(opened, saved, typed):
    m._set_setting(m.GYM_CODE_KEY, saved)
    with m.app.test_client() as c:
        assert _register(c, code=typed).status_code == 200
        assert _register(c, uid="u-other", code="ちがう").status_code == 401
