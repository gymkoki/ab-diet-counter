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
def client():
    m.init_db()
    m.app.config["TESTING"] = True

    def clean():
        for t in ("accounts", "account_reset_codes", "member_devices"):
            _exec(f"DELETE FROM {t}")
        _exec(f"DELETE FROM daily_b_count WHERE user_id LIKE {m.PH}", ("acc-%",))
        m._login_fails.clear()
    clean()
    with m.app.test_client() as c:
        yield c
    clean()


def _register(c, uid="acc-1", email=EMAIL, pw=PW, headers=None):
    return c.post("/api/account/register", json={"user_id": uid, "email": email, "password": pw},
                  headers=headers or {})


# ── ① 登録しても記録はそのまま ─────────────────────────────────
def test_register_binds_current_records(client):
    _exec(f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) VALUES ({m.PH},'2026-09-01',3,'x')", ("acc-1",))
    r = _register(client)
    assert r.status_code == 200 and r.get_json()["email"] == "ha****@example.com"
    rows = _q(f"SELECT email, user_id, password_hash FROM accounts")
    assert rows[0][0] == "hanako@example.com" and rows[0][1] == "acc-1", "メールは小文字にそろえて保存"
    assert PW not in rows[0][2] and rows[0][2].startswith(("scrypt:", "pbkdf2:")), "パスワードはハッシュだけ"
    # 記録は user_id のまま（移し替えなし）
    assert _q(f"SELECT COUNT(*) FROM daily_b_count WHERE user_id={m.PH}", ("acc-1",))[0][0] == 1
    s = client.get("/api/account/status?user_id=acc-1").get_json()
    assert s["registered"] is True and s["email"] == "ha****@example.com"
    assert client.get("/api/account/status?user_id=other").get_json()["registered"] is False


@pytest.mark.parametrize("email,pw,msg", [
    ("not-an-email", PW, "形式"),
    (EMAIL, "short", "8文字以上"),
])
def test_register_validates_input(client, email, pw, msg):
    r = _register(client, email=email, pw=pw)
    assert r.status_code == 400 and msg in r.get_json()["error"]


def test_same_email_or_same_device_twice_is_refused(client):
    assert _register(client).status_code == 200
    r = _register(client, uid="acc-2", email="hanako@example.com")
    assert r.status_code == 409 and r.get_json()["email_taken"] is True
    assert _register(client, email="other@example.com").status_code == 409


# ── ② 別の端末でログイン ─────────────────────────────────────
def test_login_returns_same_records_and_shares_device_pass(client):
    m._device_token_for("acc-1")                      # 1台目は合言葉を通っている
    first = _q(f"SELECT token FROM member_devices WHERE user_id={m.PH}", ("acc-1",))[0][0]
    _register(client)
    r = client.post("/api/account/login", json={"email": " HANAKO@example.com ", "password": PW})
    d = r.get_json()
    assert r.status_code == 200 and d["user_id"] == "acc-1"
    assert d["token"] == first, "1台目の通行証を取り上げない（同じ通行証を共有する）"
    assert "display_name" in d["profile"]


def test_login_issues_pass_when_none_yet(client):
    _register(client)
    d = client.post("/api/account/login", json={"email": EMAIL, "password": PW}).get_json()
    assert d["token"] and m._is_unlocked("acc-1", d["token"]), "ログインした端末は合言葉なしで使える"


# ── ③ 間違い・ロック・登録の有無を教えない ─────────────────────────
def test_wrong_password_and_unknown_email_look_the_same(client):
    _register(client)
    a = client.post("/api/account/login", json={"email": EMAIL, "password": "wrongpass"})
    b = client.post("/api/account/login", json={"email": "nobody@example.com", "password": PW})
    assert a.status_code == b.status_code == 401
    assert a.get_json()["error"] == b.get_json()["error"]


def test_login_locks_after_many_failures(client):
    _register(client)
    for _ in range(m.ACCOUNT_LOGIN_MAX_FAILS):
        client.post("/api/account/login", json={"email": EMAIL, "password": "wrongpass"})
    r = client.post("/api/account/login", json={"email": EMAIL, "password": PW})
    assert r.status_code == 429, "正しいパスワードでもロック中は入れない（総当たり対策）"


def test_password_reset_by_mailed_code(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(m, "_send_reset_mail", lambda email, code: sent.update(email=email, code=code))
    _register(client)
    r = client.post("/api/account/password-reset/request", json={"email": EMAIL})
    assert r.status_code == 200 and sent["email"] == "hanako@example.com" and re.fullmatch(r"\d{6}", sent["code"])
    stored = _q("SELECT code_hash FROM account_reset_codes")[0][0]
    assert sent["code"] not in stored, "確認コードもハッシュだけを保存"
    bad = client.post("/api/account/password-reset/confirm",
                      json={"email": EMAIL, "code": "000000" if sent["code"] != "000000" else "111111", "password": "newpass99"})
    assert bad.status_code == 400
    ok = client.post("/api/account/password-reset/confirm",
                     json={"email": EMAIL, "code": sent["code"], "password": "newpass99"})
    assert ok.status_code == 200 and ok.get_json()["user_id"] == "acc-1"
    assert client.post("/api/account/login", json={"email": EMAIL, "password": "newpass99"}).status_code == 200
    assert client.post("/api/account/login", json={"email": EMAIL, "password": PW}).status_code == 401
    # 使ったコードは二度と使えない
    again = client.post("/api/account/password-reset/confirm",
                        json={"email": EMAIL, "code": sent["code"], "password": "another99"})
    assert again.status_code == 400


def test_reset_code_gives_up_after_too_many_tries(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(m, "_send_reset_mail", lambda email, code: sent.update(code=code))
    _register(client)
    client.post("/api/account/password-reset/request", json={"email": EMAIL})
    wrong = "000000" if sent["code"] != "000000" else "111111"
    for _ in range(m.ACCOUNT_RESET_MAX_ATTEMPTS):
        client.post("/api/account/password-reset/confirm", json={"email": EMAIL, "code": wrong, "password": "newpass99"})
    r = client.post("/api/account/password-reset/confirm", json={"email": EMAIL, "code": sent["code"], "password": "newpass99"})
    assert r.status_code == 400, "6桁を総当たりされないように、間違いが続いたらそのコードは使えない"


def test_reset_request_does_not_reveal_unknown_email(client, monkeypatch):
    called = []
    monkeypatch.setattr(m, "_send_reset_mail", lambda *a: called.append(a))
    r = client.post("/api/account/password-reset/request", json={"email": "nobody@example.com"})
    assert r.status_code == 200 and not called


# ── ④ 合言葉が有効なあいだは、会員の端末だけが登録できる ───────────────
def test_register_needs_member_pass_while_gate_is_on(client, monkeypatch):
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    r = _register(client)
    assert r.status_code == 403 and r.get_json()["unlock_required"] is True
    token = m._device_token_for("acc-1")
    assert _register(client, headers={"X-Device-Token": token}).status_code == 200


# ── ⑤ 有料化が始まったら合言葉は終わる／お申し込みはアカウントから ─────────
def test_member_gate_ends_when_billing_starts(monkeypatch):
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    monkeypatch.setattr(m, "_billing_active", lambda today=None: False)
    assert m._member_gate_active() is True
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    assert m._member_gate_active() is False


def test_member_gate_stays_when_billing_check_breaks(monkeypatch):
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")

    def boom(today=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "_billing_active", boom)
    assert m._member_gate_active() is True, "判定できないときは今までどおり合言葉で守る（無料で開放しない）"


def test_checkout_requires_account(client, monkeypatch):
    monkeypatch.setattr(m, "_billing_active", lambda today=None: True)
    monkeypatch.setattr(m, "_billing_configured", lambda: True)
    r = client.post("/api/billing/checkout", json={"user_id": "acc-1", "agree": True})
    assert r.status_code == 403 and r.get_json()["account_required"] is True


def test_admin_sees_account_count(client):
    _register(client)
    d = client.get("/api/admin/member-gate", headers=ADMIN).get_json()
    assert d["accounts"] == 1


# ── 画面 ────────────────────────────────────────────────────
def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def test_settings_has_account_card_and_gate_has_login():
    h = _html()
    card = h[h.index('id="account-card"'):]
    card = card[:card.index('<div class="setting-card">')]
    for kw in ("registerAccount()", "loginAccount()", "requestPasswordReset()", "confirmPasswordReset()",
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
