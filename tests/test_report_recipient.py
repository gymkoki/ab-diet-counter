# デイリーレポート／バックアップメールの宛先の回帰テスト。
# オーナー指示（2026-08）：rits.1159@gmail.com へ送る。reallgym.tokyo 宛には送らない。
# GitHub Secrets や設定画面に古いアドレスが残っていても、コード側で振り替えることを固定する。

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")

import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED = "rits.1159@gmail.com"
BLOCKED = "reallgym.tokyo@gmail.com"


def _load_send_report_resolver():
    """report/send_report.py は matplotlib 等の重い依存を読むため、
    宛先解決のロジック部分だけを取り出して評価する。"""
    src = open(os.path.join(ROOT, "report", "send_report.py"), encoding="utf-8").read()
    start = src.index("def _clean_secret")
    end = src.index("JST = datetime.timezone")
    ns = {"os": os}
    exec(src[start:end], ns)  # noqa: S102 - テスト内での限定的な実行
    return ns["_resolve_report_to"]


def test_app_defaults_to_rits_when_unset():
    """設定が空なら rits.1159@gmail.com に送る。"""
    m._set_setting("REPORT_TO", "")
    assert m._resolve_report_to() == EXPECTED


def test_app_never_sends_to_reallgym():
    """設定に reallgym.tokyo が残っていても、そこには送らない。"""
    for bad in (BLOCKED, BLOCKED.upper(), f"  {BLOCKED}  "):
        m._set_setting("REPORT_TO", bad)
        got = m._resolve_report_to()
        assert got == EXPECTED, f"reallgym 宛に送信されます（{bad!r} → {got}）"


def test_app_keeps_other_explicit_address():
    """明示的に指定した別アドレスはそのまま使う（将来の宛先変更を妨げない）。"""
    m._set_setting("REPORT_TO", "someone@example.com")
    assert m._resolve_report_to() == "someone@example.com"
    m._set_setting("REPORT_TO", EXPECTED)
    assert m._resolve_report_to() == EXPECTED


def test_github_actions_report_never_sends_to_reallgym():
    """毎朝8時に実際に送る report/send_report.py 側も同じ扱いであること。"""
    resolve = _load_send_report_resolver()
    assert resolve("") == EXPECTED
    for bad in (BLOCKED, BLOCKED.upper(), f" {BLOCKED} "):
        assert resolve(bad) == EXPECTED, f"reallgym 宛に送信されます（{bad!r}）"
    assert resolve(EXPECTED) == EXPECTED
    assert resolve("someone@example.com") == "someone@example.com"


def test_feedback_is_sent_to_rits():
    """修正希望・ご要望（利用者からの問い合わせ）の転送先も rits.1159@gmail.com であること。"""
    assert m.FEEDBACK_TO == EXPECTED, f"問い合わせが {m.FEEDBACK_TO} に送られます"


def test_feedback_email_actually_addressed_to_rits(monkeypatch):
    """実際に /api/feedback を叩いたとき、SMTPへ渡される宛先が rits であること。"""
    m._set_setting("GMAIL_USER", "sender@example.com")
    m._set_setting("GMAIL_APP_PASSWORD", "dummy-pass")
    captured = {}

    class _SMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def sendmail(self, from_addr, to_addrs, msg):
            captured["to"] = to_addrs
            captured["raw"] = msg

    monkeypatch.setattr(m.smtplib, "SMTP_SSL", _SMTP)
    c = m.app.test_client()
    r = c.post("/api/feedback", json={"user_id": "u-1", "text": "テストの要望です"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert captured.get("to") == [EXPECTED], f"転送先が想定外です: {captured.get('to')}"
    assert BLOCKED.encode() not in captured.get("raw", b""), \
        "メール本文/ヘッダに reallgym 宛が残っています"


def test_no_hardcoded_reallgym_fallback_remains():
    """レポート送信のフォールバック先に reallgym.tokyo が残っていないこと。"""
    app_src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    rep_src = open(os.path.join(ROOT, "report", "send_report.py"), encoding="utf-8").read()
    bad_fallback = f'or "{BLOCKED}"'
    assert bad_fallback not in app_src, "app.py にreallgym宛のフォールバックが残っています"
    assert bad_fallback not in rep_src, "send_report.py にreallgym宛のフォールバックが残っています"
    # 送信禁止リストとして参照されているのは可
    assert BLOCKED in m.REPORT_TO_BLOCKED
    assert m.REPORT_TO_DEFAULT == EXPECTED
