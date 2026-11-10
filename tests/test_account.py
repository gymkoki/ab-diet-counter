# アカウント（メールアドレス＋パスワード）の回帰テスト（オーナー指示 2026-11-10）。
#
# 「将来はメール・パスワードのアカウントから有料化の手続きをしたい。今までの履歴を有料化後も引き継げるように」
# 「ログインはメール＋パスワード」「アカウント制にしたら合言葉は残さない」。
#
# 守る約束：
#   ①登録は「今の端末の user_id にメールとパスワードを結びつける」だけ。記録は動かさない（＝そのまま引き継がれる）
#   ②別の端末でログインすると、同じ user_id と通行証（合言葉の代わり）が返る。前の端末の通行証は取り上げない
#   ③パスワードはハッシュだけを保存する。間違いが続いたらロック。登録の有無を他人に教えない
#   ④合言葉が有効なあいだ、登録は合言葉を通った端末だけ（外の人がアカウントを作れない）
#   ⑤有料化が始まったら合言葉は自動で終わる。お申し込みはアカウント登録が先
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
EMAIL = "Hanako@Example.com"
PW = "abcd1234"


def _exec(sql, params=()):
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _q(sql, params=()):
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


@pytest.fixture
def mails(monkeypatch):
    """送ったメール（宛先・件名・本文）を記録する。本物の Gmail には送らない。"""
    sent = []
    monkeypatch.setattr(m, "_send_account_mail", lambda email, subject, body: sent.append((email, subject, body)))
    return sent


@pytest.fixture
def client(mails):
    m.init_db()
    m.app.config["TESTING"] = True

    def clean():
        for t in ("accounts", "account_reset_codes", "account_pending", "member_devices"):
            _exec(f"DELETE FROM {t}")
        _exec(f"DELETE FROM daily_b_count WHERE user_id LIKE {m.PH}", ("acc-%",))
        m._login_fails.clear()
        m._mail_sends.clear()
    clean()
    with m.app.test_client() as c:
        yield c
    clean()


def _code(mails):
    return re.search(r"確認コード：(\d{6})", mails[-1][2]).group(1)


def _register(c, uid="acc-1", email=EMAIL, pw=PW, headers=None):
    return c.post("/api/account/register", json={"user_id": uid, "email": email, "password": pw},
                  headers=headers or {})


def _confirm(c, code, uid="acc-1", headers=None):
    return c.post("/api/account/register/confirm", json={"user_id": uid, "code": code}, headers=headers or {})


def _register_full(c, mails, uid="acc-1", email=EMAIL, pw=PW, headers=None):
    r = _register(c, uid, email, pw, headers)
    assert r.status_code == 200, r.get_json()
    r = _confirm(c, _code(mails), uid, headers)
    assert r.status_code == 200, r.get_json()
    m._mail_sends.clear()
    return r


# ── ① 登録しても記録はそのまま（メールの確認コードで仕上げる） ─────────────
def test_register_binds_current_records(client, mails):
    _exec(f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) VALUES ({m.PH},'2026-09-01',3,'x')", ("acc-1",))
    r = _register(client)
    assert r.status_code == 200 and r.get_json()["pending"] is True
    assert mails[-1][0] == "hanako@example.com" and "確認コード" in mails[-1][1]
    assert _q("SELECT COUNT(*) FROM accounts")[0][0] == 0, "確認コードを入れるまでアカウントは作らない"
    assert _code(mails) not in _q("SELECT code_hash FROM account_pending")[0][0], "確認コードもハッシュだけ"
    d = _confirm(client, _code(mails)).get_json()
    assert d["user_id"] == "acc-1" and d["email"] == "ha****@example.com" and d["token"]
    rows = _q("SELECT email, user_id, password_hash FROM accounts")
    assert rows[0][0] == "hanako@example.com" and rows[0][1] == "acc-1", "メールは小文字にそろえて保存"
    assert PW not in rows[0][2] and rows[0][2].startswith(("scrypt:", "pbkdf2:")), "パスワードはハッシュだけ"
    # 記録は user_id のまま（移し替えなし）
    assert _q(f"SELECT COUNT(*) FROM daily_b_count WHERE user_id={m.PH}", ("acc-1",))[0][0] == 1
    s = client.get("/api/account/status?user_id=acc-1").get_json()
    assert s["registered"] is True and s["email"] == "ha****@example.com"
    assert client.get("/api/account/status?user_id=other").get_json()["registered"] is False


def test_wrong_registration_code_is_refused(client, mails):
    _register(client)
    wrong = "000000" if _code(mails) != "000000" else "111111"
    for _ in range(m.ACCOUNT_RESET_MAX_ATTEMPTS):
        assert _confirm(client, wrong).status_code == 400
    assert _confirm(client, _code(mails)).status_code == 400, "間違いが続いたらそのコードは使えない"
    assert _q("SELECT COUNT(*) FROM accounts")[0][0] == 0


@pytest.mark.parametrize("email,pw,msg", [
    ("not-an-email", PW, "形式"),
    (EMAIL, "short", "8文字以上"),
])
def test_register_validates_input(client, email, pw, msg):
    r = _register(client, email=email, pw=pw)
    assert r.status_code == 400 and msg in r.get_json()["error"]


def test_registered_email_is_not_revealed_by_register(client, mails):
    """登録済みのアドレスでも、画面の応答は同じ（持ち主には「登録済みです」のメールが届く）。"""
    _register_full(client, mails)
    fresh = _register(client, uid="acc-3", email="new@example.com").get_json()
    taken = _register(client, uid="acc-2", email="hanako@example.com").get_json()
    assert taken == fresh, "登録の有無が応答から分からないこと"
    assert "登録済み" in mails[-1][1] and "確認コード：" not in mails[-1][2]
    assert _q(f"SELECT COUNT(*) FROM account_pending WHERE user_id={m.PH}", ("acc-2",))[0][0] == 0


def test_same_device_twice_is_refused(client, mails):
    token = _register_full(client, mails).get_json()["token"]
    r = _register(client, email="other@example.com", headers={"X-Device-Token": token})
    assert r.status_code == 409
    assert _register(client, email="other@example.com").status_code == 403, "通行証が無ければ登録の操作自体ができない"


# ── ② 別の端末でログイン ─────────────────────────────────────
def test_login_returns_same_records_and_shares_device_pass(client, mails):
    first = m._device_token_for("acc-1")              # 1台目は合言葉を通っている
    _register_full(client, mails, headers={"X-Device-Token": first})
    r = client.post("/api/account/login", json={"email": " HANAKO@example.com ", "password": PW})
    d = r.get_json()
    assert r.status_code == 200 and d["user_id"] == "acc-1"
    assert d["token"] == first, "1台目の通行証を取り上げない（同じ通行証を共有する）"
    assert "display_name" in d["profile"]


def test_login_issues_pass_when_none_yet(client, mails):
    _register_full(client, mails)
    d = client.post("/api/account/login", json={"email": EMAIL, "password": PW}).get_json()
    assert d["token"] and m._is_unlocked("acc-1", d["token"]), "ログインした端末は合言葉なしで使える"


# ── ③ 間違い・ロック・登録の有無を教えない・メールの送りすぎを防ぐ ───────────────
def test_wrong_password_and_unknown_email_look_the_same(client, mails):
    _register_full(client, mails)
    a = client.post("/api/account/login", json={"email": EMAIL, "password": "wrongpass"})
    b = client.post("/api/account/login", json={"email": "nobody@example.com", "password": PW})
    assert a.status_code == b.status_code == 401
    assert a.get_json()["error"] == b.get_json()["error"]


def test_login_locks_after_many_failures(client, mails):
    _register_full(client, mails)
    for _ in range(m.ACCOUNT_LOGIN_MAX_FAILS):
        client.post("/api/account/login", json={"email": EMAIL, "password": "wrongpass"})
    r = client.post("/api/account/login", json={"email": EMAIL, "password": PW})
    assert r.status_code == 429, "正しいパスワードでもロック中は入れない（総当たり対策）"


def test_own_successful_login_does_not_reset_lock(client, mails):
    """自分のアカウントで成功を挟んでも、他人への推測の失敗回数は消えない（Codex 指摘）。"""
    _register_full(client, mails)
    _register_full(client, mails, uid="acc-2", email="victim@example.com", pw="victimpass1")
    for _ in range(3):
        for _ in range(m.ACCOUNT_LOGIN_MAX_FAILS // 3 + 1):
            client.post("/api/account/login", json={"email": "victim@example.com", "password": "guess0000"})
        client.post("/api/account/login", json={"email": EMAIL, "password": PW})
    r = client.post("/api/account/login", json={"email": "victim@example.com", "password": "victimpass1"})
    assert r.status_code == 429


def test_password_reset_by_mailed_code(client, mails):
    _register_full(client, mails)
    r = client.post("/api/account/password-reset/request", json={"email": EMAIL})
    assert r.status_code == 200 and mails[-1][0] == "hanako@example.com"
    code = _code(mails)
    stored = _q("SELECT code_hash FROM account_reset_codes")[0][0]
    assert code not in stored, "確認コードもハッシュだけを保存"
    bad = client.post("/api/account/password-reset/confirm",
                      json={"email": EMAIL, "code": "000000" if code != "000000" else "111111", "password": "newpass99"})
    assert bad.status_code == 400
    ok = client.post("/api/account/password-reset/confirm", json={"email": EMAIL, "code": code, "password": "newpass99"})
    assert ok.status_code == 200 and ok.get_json()["user_id"] == "acc-1"
    assert client.post("/api/account/login", json={"email": EMAIL, "password": "newpass99"}).status_code == 200
    assert client.post("/api/account/login", json={"email": EMAIL, "password": PW}).status_code == 401
    again = client.post("/api/account/password-reset/confirm", json={"email": EMAIL, "code": code, "password": "another99"})
    assert again.status_code == 400, "使ったコードは二度と使えない"


def test_reset_code_gives_up_after_too_many_tries(client, mails):
    _register_full(client, mails)
    client.post("/api/account/password-reset/request", json={"email": EMAIL})
    code = _code(mails)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(m.ACCOUNT_RESET_MAX_ATTEMPTS):
        client.post("/api/account/password-reset/confirm", json={"email": EMAIL, "code": wrong, "password": "newpass99"})
    r = client.post("/api/account/password-reset/confirm", json={"email": EMAIL, "code": code, "password": "newpass99"})
    assert r.status_code == 400, "6桁を総当たりされないように、間違いが続いたらそのコードは使えない"


def test_reset_request_does_not_reveal_unknown_email(client, mails):
    r = client.post("/api/account/password-reset/request", json={"email": "nobody@example.com"})
    assert r.status_code == 200 and not mails


def test_confirmation_mails_are_rate_limited(client, mails):
    """同じ宛先へ続けて送れない（受信箱への大量送信・届いたコードを次々に無効にする嫌がらせの対策）。"""
    _register_full(client, mails)
    assert client.post("/api/account/password-reset/request", json={"email": EMAIL}).status_code == 200
    r = client.post("/api/account/password-reset/request", json={"email": EMAIL})
    assert r.status_code == 429 and len([x for x in mails if "再設定" in x[1]]) == 1


def test_confirmation_mails_are_limited_per_ip(client, mails, monkeypatch):
    monkeypatch.setattr(m, "ACCOUNT_MAIL_MAX_PER_IP", 3)
    codes = [client.post("/api/account/password-reset/request", json={"email": f"x{i}@example.com"}).status_code
             for i in range(4)]
    assert codes == [200, 200, 200, 429]


# ── ④ その記録の持ち主の端末だけが登録できる ─────────────────────────
def test_register_needs_member_pass_while_gate_is_on(client, mails, monkeypatch):
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    r = _register(client)
    assert r.status_code == 403 and r.get_json()["unlock_required"] is True
    token = m._device_token_for("acc-1")
    _register_full(client, mails, headers={"X-Device-Token": token})


def test_cannot_register_someone_elses_records(client, mails, monkeypatch):
    """通行証を持つ記録（＝会員の記録）は、IDを知っていても他人が登録できない（有料化後も）。"""
    m._device_token_for("acc-1")
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    assert _register(client).status_code == 403
    assert _register(client, headers={"X-Device-Token": "guess"}).status_code == 403
    assert not mails


def test_old_records_without_pass_cannot_be_claimed_after_billing(client, monkeypatch):
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    monkeypatch.setattr(m, "_billing_start_date", lambda: datetime.date(2026, 12, 1))
    monkeypatch.setattr(m, "_first_activity_date", lambda uid: datetime.date(2026, 10, 1))
    assert _register(client).status_code == 403
    monkeypatch.setattr(m, "_first_activity_date", lambda uid: None)    # 有料化後に始めた人は登録できる
    assert _register(client).status_code == 200


# ── ⑤ 有料化が始まったら合言葉は終わり、AIの入口はアカウントで守る ───────────
def test_member_gate_ends_when_billing_starts(monkeypatch):
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    monkeypatch.setattr(m, "_billing_active", lambda today=None: False)
    assert m._member_gate_active() is True and m._gate_mode() == "passphrase"
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    assert m._member_gate_active() is False and m._gate_mode() == "account"


def test_member_gate_stays_when_billing_check_breaks(monkeypatch):
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")

    def boom(today=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "_billing_active", boom)
    assert m._member_gate_active() is True, "判定できないときは今までどおり合言葉で守る（無料で開放しない）"


@pytest.mark.parametrize("path", ["/analyze-text", "/analyze", "/reanalyze", "/estimate-nutrition"])
def test_ai_needs_account_after_billing_starts(client, mails, monkeypatch, path):
    """有料化後、アカウントの無い新しいIDではAIを使えない（IDを作り直して無料期間を繰り返せない。Codex 指摘）。"""
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    called = []
    monkeypatch.setattr(m, "get_client", lambda: called.append(1) or object())
    r = client.post(path, data={"user_id": "brand-new", "text": "ごはん"})
    assert r.status_code == 403 and r.get_json()["account_required"] is True and not called
    # 合言葉の通行証だけ（アカウント無し）でも通さない
    tok = m._device_token_for("acc-9")
    r = client.post(path, data={"user_id": "acc-9", "text": "ごはん"}, headers={"X-Device-Token": tok})
    assert r.status_code == 403 and r.get_json()["account_required"] is True


def test_account_holder_passes_the_gate_after_billing(client, mails, monkeypatch):
    _register_full(client, mails)
    token = client.post("/api/account/login", json={"email": EMAIL, "password": PW}).get_json()["token"]
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    with m.app.test_request_context(headers={"X-Device-Token": token}):
        assert m._member_gate_blocked("acc-1") is None
    s = client.get(f"/api/member-status?user_id=acc-1&token={token}").get_json()
    assert s["account_required"] is False and s["gate_active"] is False
    s2 = client.get("/api/member-status?user_id=someone").get_json()
    assert s2["account_required"] is True


def test_billing_portal_still_needs_device_pass_after_billing(client, monkeypatch):
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    r = client.post("/api/billing/portal", json={"user_id": "acc-1"})
    assert r.status_code == 403, "合言葉が終わっても、他人の支払いの画面は開けない"


def test_checkout_requires_account(client, monkeypatch):
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    monkeypatch.setattr(m, "_billing_configured", lambda: True)
    r = client.post("/api/billing/checkout", json={"user_id": "acc-1", "agree": True})
    assert r.status_code == 403 and r.get_json()["account_required"] is True


def test_admin_sees_account_count(client, mails):
    _register_full(client, mails)
    d = client.get("/api/admin/member-gate", headers=ADMIN).get_json()
    assert d["accounts"] == 1


def test_accounts_are_in_the_backup():
    """バックアップから戻したあとも、メールとパスワードで同じ記録にログインできるように（Codex 指摘）。"""
    assert "accounts" in m._BACKUP_TABLES
    assert "account_pending" not in m._BACKUP_TABLES and "account_reset_codes" not in m._BACKUP_TABLES


# ── 画面 ────────────────────────────────────────────────────
def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def test_settings_has_account_card_and_gate_has_login():
    h = _html()
    card = h[h.index('id="account-card"'):]
    card = card[:card.index('<div class="setting-card">')]
    for kw in ("registerAccount()", "confirmRegistration()", "loginAccount()", "requestPasswordReset()", "confirmPasswordReset()",
               'type="password"', 'autocomplete="email"'):
        assert kw in card, kw
    gate = h[h.index('id="member-gate-overlay"'):]
    gate = gate[:gate.index("</div>\n</div>")]
    assert "openAccountLoginFromGate()" in gate, "機種変更した会員が合言葉の代わりにログインできる"


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_login_switches_device_to_account():
    h = _html()
    fn = re.search(r"async function _adoptAccountLogin\(d\) \{.*?\n\}", h, re.S).group(0)
    harness = f"""
let calls = [];
let USER_ID = 'old-uid';
async function adoptLinkedAccount(uid, prof) {{ calls.push(['adopt', uid, prof.display_name]); }}
function setMemberToken(t) {{ calls.push(['token', t]); }}
{fn}
(async () => {{
  await _adoptAccountLogin({{ user_id: 'acc-1', token: 'tok', profile: {{ display_name: '花子' }} }});
  await _adoptAccountLogin({{ user_id: 'old-uid', token: 'tok2', profile: {{}} }});
  console.log(JSON.stringify(calls));
}})();
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(harness)
        path = tmp.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        calls = json.loads(proc.stdout)
    finally:
        os.unlink(path)
    assert calls[:2] == [["token", "tok"], ["adopt", "acc-1", "花子"]]
    assert calls[2:] == [["token", "tok2"]], "すでに同じ記録の端末なら切り替えない（通行証だけ受け取る）"


def test_privacy_policy_mentions_account_email():
    with m.app.test_client() as c:
        html = c.get("/legal/privacy").get_data(as_text=True)
    assert "アカウント登録の際のメールアドレスとパスワード" in html
    assert "パスワード再設定のご案内" in html


def test_admin_card_shows_account_count():
    with open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8") as f:
        a = f.read()
    assert "アカウント登録（メール・パスワード）" in a and "d.accounts" in a


def test_passphrase_cannot_issue_pass_after_billing(client, monkeypatch):
    """有料化後は合言葉で通行証を出さない（他人のIDで本人になりすませないように。Codex 指摘）。"""
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    victim = m._device_token_for("acc-1")
    r = client.post("/api/unlock", json={"user_id": "acc-1", "password": m._member_password()})
    assert r.status_code == 403 and r.get_json()["account_required"] is True and "token" not in r.get_json()
    assert m._device_token("acc-1") == victim, "本人の通行証は上書きされない"


def test_passphrase_still_works_before_billing(client, monkeypatch):
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    monkeypatch.setattr(m, "_billing_active", lambda today=None: False)
    r = client.post("/api/unlock", json={"user_id": "acc-1", "password": m._member_password()})
    assert r.status_code == 200 and r.get_json()["token"]


def test_unknown_email_still_runs_password_check(client, mails, monkeypatch):
    """未登録のアドレスでもパスワードの照合を同じようにする（応答時間で登録の有無が分からないように。Codex 指摘）。"""
    seen = []
    real = m.check_password_hash
    monkeypatch.setattr(m, "check_password_hash", lambda h, p: seen.append(h) or real(h, p))
    client.post("/api/account/login", json={"email": "nobody@example.com", "password": "whatever1"})
    assert seen == [m._DUMMY_PASSWORD_HASH]
