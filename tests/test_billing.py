"""有料化（月額1,100円・Stripe）の回帰テスト（オーナー指示 2026-10-13）。

ここで固定する不変条件：
  ①Stripeの鍵が無い／開始日前／緊急停止中は、誰も止めない・会員に何も表示しない
  ②開始日以降、既存の会員（開始日前に記録がある人）は申し込まないと解析できない（無料期間なし）
  ③初めて使う人は、最初に使った日から3日間は無料
  ④申し込み済み（active / trialing / past_due）は使える。解約で期間が終わったら止まる
  ⑤判定中にDBが壊れていても止めない
  ⑥AIを呼ぶ4つの入口すべてで、AIを呼ぶ前に止める
  ⑦Stripeからの通知は署名を確かめ、支払い状況に反映する
  ⑧申し込みは利用規約・プライバシーポリシーへの同意が必須
  ⑨特商法・規約・プライバシーのページが開ける／管理APIは管理者だけ
"""
import datetime
import io
import json
import os
import re
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
os.environ.setdefault("ADMIN_PASSWORD", "testpw")

import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = datetime.date(2026, 11, 1)


def _reset():
    m.init_db()
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        for t in ("billing_accounts", "daily_b_count", "daily_weight", "daily_meals",
                  "daily_nutrition", "usage_log"):
            cur.execute(f"DELETE FROM {t}")
        cur.execute("DELETE FROM settings WHERE key IN ('billing_preorder_open','billing_disabled','legal_business_info')")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def on(monkeypatch):
    """Stripe の鍵あり・開始日 2026-11-01 の状態にする。"""
    _reset()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(m, "BILLING_START", START.isoformat())
    yield
    _reset()


def _record(uid, date):
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) VALUES ({m.PH},{m.PH},{m.PH},{m.PH})",
                    (uid, date.isoformat(), 3.0, date.isoformat()))
        conn.commit()
    finally:
        conn.close()


# ── ① 何もしない条件 ──────────────────────────────────────
def test_off_without_stripe_key():
    _reset()
    st = m.billing_state("u-x", today=datetime.date(2026, 12, 1))
    assert st["state"] == "off" and st["required"] is False and st["enabled"] is False


def test_off_without_webhook_secret(on, monkeypatch):
    """Webhook が無いと支払いが記録されず払った会員まで止まるので、片方だけなら何もしない。"""
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET")
    _record("u-old", datetime.date(2026, 9, 1))
    assert m.billing_state("u-old", today=datetime.date(2026, 11, 2))["state"] == "off"


def test_off_before_start(on):
    _record("u-old", datetime.date(2026, 9, 1))
    st = m.billing_state("u-old", today=datetime.date(2026, 10, 31))
    assert st["state"] == "off" and not st["required"]


def test_off_when_emergency_stopped(on):
    m._set_setting(m.BILLING_DISABLED_KEY, "1")
    _record("u-old", datetime.date(2026, 9, 1))
    assert m.billing_state("u-old", today=datetime.date(2026, 11, 2))["state"] == "off"


def test_broken_start_date_disables(on, monkeypatch):
    monkeypatch.setattr(m, "BILLING_START", "2026-13-40")
    assert m._billing_active(datetime.date(2027, 1, 1)) is False


# ── ② 既存の会員は無料期間なし ─────────────────────────────
def test_existing_member_must_pay_from_start(on):
    _record("u-old", datetime.date(2026, 9, 1))
    st = m.billing_state("u-old", today=START)
    assert st["state"] == "required" and st["required"] is True


# ── ③ 初めての人は3日間無料 ───────────────────────────────
def test_new_user_gets_three_free_days(on):
    first = datetime.date(2026, 11, 5)
    _record("u-new", first)
    for d in range(3):
        st = m.billing_state("u-new", today=first + datetime.timedelta(days=d))
        assert st["state"] == "trial", f"{d}日目なのに無料期間ではない"
        assert st["trial_ends"] == "2026-11-08"
    assert m.billing_state("u-new", today=first + datetime.timedelta(days=3))["state"] == "required"


def test_brand_new_user_without_records_starts_trial_today(on):
    st = m.billing_state("u-blank", today=datetime.date(2026, 11, 10))
    assert st["state"] == "trial"
    # 2回目以降も同じ日を起点にする（開くたびに無料期間が延びない）
    assert m.billing_state("u-blank", today=datetime.date(2026, 11, 13))["state"] == "required"


# ── ④ 申し込み済みは使える ────────────────────────────────
@pytest.mark.parametrize("status,allowed", [
    ("active", True), ("trialing", True), ("past_due", True),
    ("canceled", False), ("unpaid", False), ("incomplete_expired", False),
])
def test_subscription_status(on, status, allowed):
    _record("u-sub", datetime.date(2026, 9, 1))
    m._upsert_billing("u-sub", customer_id="cus_1", subscription_id="sub_1", status=status,
                      current_period_end="2026-12-01T00:00:00+09:00", cancel_at_period_end=0)
    st = m.billing_state("u-sub", today=datetime.date(2026, 11, 2))
    assert (st["state"] == "subscribed") is allowed
    assert st["required"] is (not allowed)
    assert st["can_manage"] is True


def test_preorder_only_when_owner_opens_it(on):
    _record("u-old", datetime.date(2026, 9, 1))
    assert m.billing_state("u-old", today=datetime.date(2026, 10, 25))["state"] == "off"
    m._set_setting(m.BILLING_PREORDER_KEY, "1")
    st = m.billing_state("u-old", today=datetime.date(2026, 10, 25))
    assert st["state"] == "preorder" and st["required"] is False


# ── ⑤ DBが壊れていても止めない ─────────────────────────────
def test_db_error_does_not_block(on, monkeypatch):
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "billing_state", boom)
    with m.app.test_request_context():
        assert m._billing_blocked("u-any") is None


# ── ⑥ AIを呼ぶ入口で止める ─────────────────────────────────
@pytest.mark.parametrize("path,form", [
    ("/analyze", {"image": (io.BytesIO(b"x"), "a.jpg")}),
    ("/reanalyze", {"correction": "白米→75g"}),
    ("/analyze-text", {"text": "カレー"}),
    ("/estimate-nutrition", {"text": "カレー"}),
])
def test_ai_endpoints_blocked_before_calling_ai(on, monkeypatch, path, form):
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    _record("u-old", datetime.date(2026, 9, 1))
    called = {"n": 0}

    def fake_client():
        called["n"] += 1
        return None
    monkeypatch.setattr(m, "get_client", fake_client)
    with m.app.test_client() as c:
        r = c.post(path, data=dict(form, user_id="u-old"), content_type="multipart/form-data")
    assert r.status_code == 402, f"{path} が止まっていない"
    assert r.get_json()["billing_required"] is True
    assert called["n"] == 0, f"{path} が止める前にAIの準備をしている"


def test_ai_endpoints_pass_when_billing_off(monkeypatch):
    _reset()
    called = {"n": 0}
    monkeypatch.setattr(m, "get_client", lambda: called.__setitem__("n", called["n"] + 1) or None)
    with m.app.test_client() as c:
        r = c.post("/analyze-text", data={"text": "カレー", "user_id": "u-x"})
    assert r.status_code != 402 and called["n"] == 1


# ── ⑦ Stripeからの通知 ────────────────────────────────────
class _FakeStripe:
    def __init__(self, event, sub=None, fail_sig=False):
        self._event, self._sub, self._fail = event, sub, fail_sig
        outer = self

        class Webhook:
            @staticmethod
            def construct_event(payload, sig, secret):
                if outer._fail or secret != "whsec_test":
                    raise ValueError("bad signature")
                return outer._event

        class Subscription:
            @staticmethod
            def retrieve(sid):
                return outer._sub

        self.Webhook, self.Subscription = Webhook, Subscription


def _post_webhook(monkeypatch, fake):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(m, "_stripe_lib", lambda: fake)
    with m.app.test_client() as c:
        return c.post("/api/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})


def test_webhook_rejects_bad_signature(on, monkeypatch):
    r = _post_webhook(monkeypatch, _FakeStripe({}, fail_sig=True))
    assert r.status_code == 400


def test_webhook_checkout_completed_activates(on, monkeypatch):
    period_end = int(datetime.datetime(2026, 12, 1, tzinfo=m.JST).timestamp())
    sub = {"id": "sub_9", "customer": "cus_9", "status": "active", "cancel_at_period_end": False,
           "metadata": {"user_id": "u-pay"},
           "items": {"data": [{"current_period_end": period_end}]}}
    event = {"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": "u-pay", "customer": "cus_9", "subscription": "sub_9",
        "customer_details": {"email": "pay@example.com"}}}}
    r = _post_webhook(monkeypatch, _FakeStripe(event, sub))
    assert r.status_code == 200
    row = m._billing_row("u-pay")
    assert row["status"] == "active" and row["customer_id"] == "cus_9"
    assert row["email"] == "pay@example.com"
    assert row["current_period_end"].startswith("2026-12-01")
    _record("u-pay", datetime.date(2026, 9, 1))
    assert m.billing_state("u-pay", today=datetime.date(2026, 11, 2))["state"] == "subscribed"


def test_webhook_subscription_deleted_stops_access(on, monkeypatch):
    m._upsert_billing("u-c", customer_id="cus_c", subscription_id="sub_c", status="active")
    event = {"type": "customer.subscription.deleted", "data": {"object": {
        "id": "sub_c", "customer": "cus_c", "status": "canceled", "metadata": {}}}}
    assert _post_webhook(monkeypatch, _FakeStripe(event)).status_code == 200
    assert m._billing_row("u-c")["status"] == "canceled"


# ── ⑧ 申し込み ────────────────────────────────────────────
class _FakeCheckout:
    def __init__(self):
        self.params = None
        outer = self

        class Session:
            @staticmethod
            def create(**kw):
                outer.params = kw
                return types.SimpleNamespace(url="https://checkout.stripe.com/c/test")

        self.checkout = types.SimpleNamespace(Session=Session)


def test_checkout_requires_consent(on, monkeypatch):
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    with m.app.test_client() as c:
        r = c.post("/api/billing/checkout", json={"user_id": "u-1", "agree": False})
    assert r.status_code == 400 and "同意" in r.get_json()["error"]


def test_checkout_creates_monthly_1100_yen(on, monkeypatch):
    fake = _FakeCheckout()
    monkeypatch.setattr(m, "_stripe_lib", lambda: fake)
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    with m.app.test_client() as c:
        r = c.post("/api/billing/checkout", json={"user_id": "u-1", "agree": True})
    assert r.status_code == 200 and r.get_json()["url"].startswith("https://checkout.stripe.com")
    p = fake.params
    pd = p["line_items"][0]["price_data"]
    assert p["mode"] == "subscription" and pd["currency"] == "jpy"
    assert pd["unit_amount"] == 1100 and pd["recurring"]["interval"] == "month"
    assert p["client_reference_id"] == "u-1"
    assert "解約" in p["custom_text"]["submit"]["message"]


def test_checkout_closed_before_start_unless_preorder(on, monkeypatch):
    with m.app.test_client() as c:
        r = c.post("/api/billing/checkout", json={"user_id": "u-1", "agree": True})
    assert r.status_code == 403


def test_preorder_first_charge_on_start_date(on, monkeypatch):
    fake = _FakeCheckout()
    monkeypatch.setattr(m, "_stripe_lib", lambda: fake)
    monkeypatch.setattr(m, "_billing_preorder_open", lambda today=None: True)
    with m.app.test_client() as c:
        r = c.post("/api/billing/checkout", json={"user_id": "u-1", "agree": True})
    assert r.status_code == 200
    trial_end = fake.params["subscription_data"]["trial_end"]
    assert datetime.datetime.fromtimestamp(trial_end, m.JST).date() == START


# ── ⑨ ページと管理API ─────────────────────────────────────
@pytest.mark.parametrize("page,text", [
    ("tokushoho", "特定商取引法に基づく表記"), ("terms", "利用規約"), ("privacy", "プライバシーポリシー"),
])
def test_legal_pages_render(page, text):
    _reset()
    with m.app.test_client() as c:
        r = c.get(f"/legal/{page}")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and text in body
    assert "1,100円" in body or page == "privacy"


def test_tokushoho_shows_owner_info_and_disclosure_note():
    _reset()
    m._set_setting(m.LEGAL_INFO_KEY, json.dumps({"seller_name": "テスト事業者", "manager_name": "奥松",
                                                 "email": "info@example.com"}))
    with m.app.test_client() as c:
        body = c.get("/legal/tokushoho").get_data(as_text=True)
    assert "テスト事業者" in body and "奥松" in body and "info@example.com" in body
    assert "ご請求があれば遅滞なく開示いたします" in body   # 住所・電話が空欄のとき
    _reset()


def test_privacy_discloses_foreign_providers():
    _reset()
    with m.app.test_client() as c:
        body = c.get("/legal/privacy").get_data(as_text=True)
    for name in ("Anthropic", "Stripe", "アメリカ合衆国"):
        assert name in body


def test_admin_billing_api_requires_admin():
    _reset()
    with m.app.test_client() as c:
        assert c.get("/api/admin/billing").status_code == 404
        assert c.post("/api/admin/billing/settings", json={"disabled": True}).status_code == 404
        r = c.get("/api/admin/billing", headers=ADMIN)
        assert r.status_code == 200 and r.get_json()["price_jpy"] == 1100


# ── 会員の画面：開始前は何も出ない ──────────────────────────
def test_member_ui_hidden_until_enabled():
    html = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
    assert re.search(r'id="billing-overlay" style="display:none', html)
    assert re.search(r'id="billing-card" style="display:none', html)
    # state が off のときはカードを隠し、申し込み画面は required のときだけ自動で出す
    assert "if (!st || st.state === 'off') { card.style.display = 'none'; return; }" in html
    assert "if (_billingState && _billingState.required) openBillingOverlay(false);" in html


def test_preorder_sentence_only_before_start(monkeypatch):
    _reset()
    monkeypatch.setattr(m, "BILLING_START", "2099-11-01")
    with m.app.test_client() as c:
        assert "より前にお申し込みの場合" in c.get("/legal/tokushoho").get_data(as_text=True)
    monkeypatch.setattr(m, "BILLING_START", "2020-11-01")
    with m.app.test_client() as c:
        assert "より前にお申し込みの場合" not in c.get("/legal/tokushoho").get_data(as_text=True)
