"""デイリーレポートの「サーバー経由の送信」の回帰テスト。

経緯（2026-10-23〜24）：GitHub Actions から Gmail へのログインが拒否され
（SMTPServerDisconnected: Connection unexpectedly closed）、2日続けてレポートが届かなかった。
対策：
  ① GitHub から送れなかったら、組み立て済みのレポートをサーバーへ送り、
     サーバーの Gmail 設定（管理画面で入力）で送り直す
  ② 最新のレポートはサーバーに保存し、メールが全部だめでも管理画面で読める
  ③ Gmail のアプリパスワードは管理画面から入れ直せる（GitHub を触らなくてよい）
  ④ 465 で切れたら 587 でやり直す。パスワード違いのときはやり直さない
"""
import base64
import email
import json
import os
import smtplib
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "report"))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


class FakeSMTP:
    """smtplib.SMTP_SSL / SMTP の代わり。fail に {port: 例外} を入れるとその段階で失敗する。"""
    calls = []
    fail_connect = {}
    fail_login = {}

    def __init__(self, host, port, timeout=None):
        self.port = port
        FakeSMTP.calls.append(("connect", port))
        if port in FakeSMTP.fail_connect:
            raise FakeSMTP.fail_connect[port]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        FakeSMTP.calls.append(("starttls", self.port))

    def login(self, user, pw):
        FakeSMTP.calls.append(("login", self.port, user, pw))
        if self.port in FakeSMTP.fail_login:
            raise FakeSMTP.fail_login[self.port]

    def sendmail(self, frm, to, raw):
        FakeSMTP.calls.append(("sendmail", self.port, frm, to, raw))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.calls = []
    FakeSMTP.fail_connect = {}
    FakeSMTP.fail_login = {}
    monkeypatch.setattr(m.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(m.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    m.init_db()
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    keys = (m.LATEST_REPORT_KEY, m.REPORT_MAIL_STATUS_KEY, "GMAIL_USER", "GMAIL_APP_PASSWORD")

    def wipe():
        conn = m._get_conn()
        try:
            cur = conn.cursor()
            for k in keys:
                cur.execute(f"DELETE FROM settings WHERE key={m.PH}", (k,))
            conn.commit()
        finally:
            conn.close()
    wipe()
    yield
    wipe()


def _relay(client, github_ok, **kw):
    body = {"report_date": "2026-10-23", "subject": "[ABダイエット] デイリーレポート 2026-10-23",
            "html": '<html><img src="cid:chart_nutrition"><img src="cid:ph_good0_1"></html>',
            "images": {"chart_nutrition": PNG, "ph_good0_1": PNG},
            "github_ok": github_ok, "github_error": "SMTPServerDisconnected: Connection unexpectedly closed"}
    body.update(kw)
    return client.post("/api/admin/report-relay", json=body, headers=ADMIN)


def _sent(smtp):
    return [c for c in smtp.calls if c[0] == "sendmail"]


# ── ① GitHub で送れたときは保存だけ。送れなかったらサーバーから送る ─────────
def test_github_ok_only_stores(smtp):
    c = m.app.test_client()
    d = _relay(c, True).get_json()
    assert d["stored"] and d["sent"] and d["via"] == "github"
    assert _sent(smtp) == [], "GitHub で送れたのにサーバーからも送ると2通届いてしまう"


def test_github_failed_server_sends_to_owner(smtp):
    m._set_setting("GMAIL_USER", "sender@example.com")
    m._set_setting("GMAIL_APP_PASSWORD", "abcdabcdabcdabcd")
    c = m.app.test_client()
    d = _relay(c, False).get_json()
    assert d["sent"] and d["via"] == "server", d
    sent = _sent(smtp)
    assert len(sent) == 1
    _, port, frm, to, raw = sent[0]
    assert to == [m._resolve_report_to()], "宛先はオーナーだけ"
    assert "reallgym.tokyo@gmail.com" not in to
    msg = email.message_from_bytes(raw)
    cids = [p.get("Content-ID") for p in msg.walk() if p.get_content_maintype() == "image"]
    assert set(cids) == {"<chart_nutrition>", "<ph_good0_1>"}, "グラフと写真が添付されていない"
    st = c.get("/api/admin/report-mail", headers=ADMIN).get_json()["status"]
    assert st["ok"] and st["via"] == "server" and st["report_date"] == "2026-10-23"


def test_no_server_creds_reports_clear_error(smtp):
    c = m.app.test_client()
    d = _relay(c, False).get_json()
    assert d["stored"] and not d["sent"]
    assert "Gmail の設定がありません" in d["error"]


# ── ④ 送信のやり直し ─────────────────────────────────────────
def test_login_rejected_is_not_retried_and_explained(smtp):
    m._set_setting("GMAIL_USER", "sender@example.com")
    m._set_setting("GMAIL_APP_PASSWORD", "abcdabcdabcdabcd")
    smtp.fail_login = {465: smtplib.SMTPAuthenticationError(535, b"bad"),
                       587: smtplib.SMTPAuthenticationError(535, b"bad")}
    d = _relay(m.app.test_client(), False).get_json()
    assert not d["sent"]
    assert "アプリパスワード" in d["error"]
    assert ("connect", 587) not in smtp.calls, "パスワード違いでやり直すとロックされやすくなる"


def test_disconnect_at_login_falls_back_to_587(smtp):
    """今回の障害と同じ「ログイン中に切られた」場合は 587 でもう1回試す。"""
    m._set_setting("GMAIL_USER", "sender@example.com")
    m._set_setting("GMAIL_APP_PASSWORD", "abcdabcdabcdabcd")
    smtp.fail_login = {465: smtplib.SMTPServerDisconnected("Connection unexpectedly closed")}
    d = _relay(m.app.test_client(), False).get_json()
    assert d["sent"], d
    assert ("starttls", 587) in smtp.calls
    assert _sent(smtp)[0][1] == 587


def test_disconnect_on_both_ports_says_login(smtp):
    m._set_setting("GMAIL_USER", "sender@example.com")
    m._set_setting("GMAIL_APP_PASSWORD", "abcdabcdabcdabcd")
    err = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    smtp.fail_login = {465: err, 587: err}
    d = _relay(m.app.test_client(), False).get_json()
    assert not d["sent"] and "ログインを拒否" in d["error"]


# ── ② 最新のレポートを管理画面で読める ──────────────────────────
def test_latest_report_inlines_images(smtp):
    c = m.app.test_client()
    _relay(c, True)
    d = c.get("/api/admin/latest-report", headers=ADMIN).get_json()
    assert d["report_date"] == "2026-10-23"
    assert "cid:" not in d["html"]
    assert f'"data:image/png;base64,{PNG}"' in d["html"]


def test_resend_uses_stored_report(smtp):
    c = m.app.test_client()
    _relay(c, True)
    m._set_setting("GMAIL_USER", "sender@example.com")
    m._set_setting("GMAIL_APP_PASSWORD", "abcdabcdabcdabcd")
    r = c.post("/api/admin/report-mail/resend", headers=ADMIN)
    assert r.status_code == 200 and r.get_json()["ok"]
    assert len(_sent(smtp)) == 1


# ── ③ 管理画面からアプリパスワードを入れ直す ────────────────────────
def test_settings_save_and_send(smtp):
    c = m.app.test_client()
    _relay(c, False)   # 設定が無いので失敗して保存だけされる
    r = c.post("/api/admin/report-mail/settings", headers=ADMIN,
               json={"gmail_user": " owner@example.com ", "gmail_app_password": "abcd efgh ijkl mnop"})
    assert r.status_code == 200, r.get_json()
    assert m._gmail_creds() == ("owner@example.com", "abcdefghijklmnop")
    login = [x for x in smtp.calls if x[0] == "login"][0]
    assert login[2:] == ("owner@example.com", "abcdefghijklmnop")
    assert len(_sent(smtp)) == 1


def test_settings_validation(smtp):
    c = m.app.test_client()
    r = c.post("/api/admin/report-mail/settings", headers=ADMIN,
               json={"gmail_user": "owner@example.com", "gmail_app_password": "short"})
    assert r.status_code == 400
    r = c.post("/api/admin/report-mail/settings", headers=ADMIN,
               json={"gmail_user": "", "gmail_app_password": "abcdabcdabcdabcd"})
    assert r.status_code == 400


def test_dashboard_value_beats_stale_env(monkeypatch):
    """Render の環境変数に古いパスワードが残っていても、管理画面で入れた値が使われる。"""
    monkeypatch.setenv("GMAIL_USER", "old@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "oldoldoldoldoldo")
    assert m._gmail_creds() == ("old@example.com", "oldoldoldoldoldo")
    m._set_setting("GMAIL_USER", "new@example.com")
    m._set_setting("GMAIL_APP_PASSWORD", "newnewnewnewnewn")
    assert m._gmail_creds() == ("new@example.com", "newnewnewnewnewn")


def test_endpoints_are_admin_only():
    c = m.app.test_client()
    assert c.post("/api/admin/report-relay", json={"html": "x"}).status_code == 404
    assert c.get("/api/admin/latest-report").status_code == 404
    assert c.get("/api/admin/report-mail").status_code == 404
    assert c.post("/api/admin/report-mail/resend").status_code == 404
    assert c.post("/api/admin/report-mail/settings", json={}).status_code == 404


def test_admin_card_is_wired():
    src = open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8").read()
    assert 'id="report-mail-card"' in src
    assert "loadReportMail()" in src and "openLatestReport()" in src and "saveReportMail()" in src


# ── send_report.py：GitHub で送れなかったらサーバーへ回す ───────────────
def _load_send_report(monkeypatch):
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report as sr
    monkeypatch.setattr(sr, "fetch_data", lambda: {"report_date": "2026-10-23", "daily_users": 1, "daily_analyses": 1})
    monkeypatch.setattr(sr, "fetch_credit_estimate", lambda: None)
    monkeypatch.setattr(sr, "build_credit_info", lambda estimate=None: {"status": "ok"})
    for fn in ("chart_weight_loss", "chart_nutrition", "chart_goal_compare"):
        monkeypatch.setattr(sr, fn, lambda data: b"png")
    monkeypatch.setattr(sr, "fetch_report_photos", lambda: None)
    monkeypatch.setattr(sr, "build_html", lambda *a, **kw: "<html>report</html>")
    monkeypatch.setattr(sr, "GMAIL_USER", "sender@example.com")
    monkeypatch.setattr(sr, "GMAIL_PASS", "abcdabcdabcdabcd")
    return sr


def test_send_report_relays_when_github_mail_fails(monkeypatch, tmp_path):
    sr = _load_send_report(monkeypatch)
    monkeypatch.chdir(tmp_path)

    def boom(*a):
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    monkeypatch.setattr(sr, "send_email", boom)
    got = {}

    def relay(subject, html, charts, rdate, github_ok, github_error=""):
        got.update(github_ok=github_ok, github_error=github_error, charts=set(charts))
        return {"stored": True, "sent": True, "via": "server"}
    monkeypatch.setattr(sr, "relay_to_server", relay)
    sr.main()   # サーバーから送れたので正常終了
    assert got["github_ok"] is False and "SMTPServerDisconnected" in got["github_error"]
    assert got["charts"] == {"chart_weight_loss", "chart_nutrition", "chart_goal_compare"}


def test_send_report_fails_loudly_when_both_fail(monkeypatch, tmp_path):
    sr = _load_send_report(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sr, "send_email", lambda *a: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(sr, "relay_to_server",
                        lambda *a, **kw: {"stored": True, "sent": False, "error": "ログインを拒否"})
    with pytest.raises(SystemExit) as ei:
        sr.main()
    assert ei.value.code == 1, "どちらからも送れないときは失敗にして GitHub の通知で気づけるようにする"


def test_send_report_github_ok_still_stores(monkeypatch, tmp_path):
    sr = _load_send_report(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sr, "send_email", lambda *a: None)
    got = {}

    def relay(*a, **kw):
        got["ok"] = a[4]
        return {"stored": True}
    monkeypatch.setattr(sr, "relay_to_server", relay)
    sr.main()
    assert got["ok"] is True


def test_relay_drops_photos_when_too_large(monkeypatch):
    sr = _load_send_report(monkeypatch)
    sent = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"stored": True}

    def post(url, data=None, headers=None, auth=None, timeout=None):
        sent["body"] = json.loads(data)
        return R()
    monkeypatch.setattr(sr.requests, "post", post)
    monkeypatch.setattr(sr, "RELAY_MAX_BYTES", 2000)
    sr.relay_to_server("s", "<html/>", {"chart_nutrition": b"a" * 10, "ph_good0_1": b"b" * 3000},
                       "2026-10-23", False, "")
    assert set(sent["body"]["images"]) == {"chart_nutrition"}
