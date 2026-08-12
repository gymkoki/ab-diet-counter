"""デイリーレポートの Claude API クレジット表示の回帰テスト。

Anthropic に「残高を返すAPI」は無いので、
Cost API（実使用額）＋ 基準残高 から残高を算出している。
その計算とフォールバック挙動が壊れていないかを検証する。
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))

import anthropic_credit as ac  # noqa: E402


TODAY = datetime.date(2026, 8, 12)


def _fake_cost(daily_usd: dict):
    """{date文字列: USD} を返す fetcher を作る。"""
    def _fetch(admin_key, start_date, timeout=30):
        assert admin_key, "Admin キーが渡されていない"
        return dict(daily_usd)
    return _fetch


# ── 基準残高のパース ────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("2026-08-01:100", (datetime.date(2026, 8, 1), 100.0)),
    (" 2026-08-01 : $120.50 ", (datetime.date(2026, 8, 1), 120.5)),
    ("2026-08-01:1,000", (datetime.date(2026, 8, 1), 1000.0)),
    ("2026-08-01：80", (datetime.date(2026, 8, 1), 80.0)),   # 全角コロン
])
def test_parse_credit_base_ok(raw, expected):
    assert ac.parse_credit_base(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "100", "2026-13-01:100", "2026-08-01:abc"])
def test_parse_credit_base_ng(raw):
    assert ac.parse_credit_base(raw) is None


# ── amount はセント単位（$1.23 = "123.45"） ──────────────────────
def test_amount_is_cents():
    assert ac._amount_to_usd("123.45") == pytest.approx(1.2345)
    assert ac._amount_to_usd(None) == 0.0


# ── 残高の算出 ──────────────────────────────────────────────────
def test_remaining_and_days_left():
    # 8/1〜8/11 の11日間、毎日 $1 使った
    daily = {f"2026-08-{d:02d}": 1.0 for d in range(1, 12)}
    info = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "sk-ant-admin01-x",
             "ANTHROPIC_CREDIT_BASE": "2026-08-01:100", "USD_JPY": "150"},
        fetcher=_fake_cost(daily),
    )
    assert info["status"] == "ok"
    assert info["spend_since_base_usd"] == pytest.approx(11.0)
    assert info["remaining_usd"] == pytest.approx(89.0)
    assert info["spend_yesterday_usd"] == pytest.approx(1.0)     # 8/11
    assert info["spend_month_usd"] == pytest.approx(11.0)
    assert info["spend_7d_avg_usd"] == pytest.approx(1.0)        # 8/5〜8/11
    assert info["days_left"] == 89
    assert info["empty_date"] == "2026-11-09"
    assert info["usd_jpy"] == 150.0
    # グラフ用の系列は日付順・31点（データが無い日は0埋め）
    assert len(info["dates"]) == 31 and len(info["values"]) == 31
    assert info["dates"][-1] == "2026-08-12"
    assert info["values"][info["dates"].index("2026-08-11")] == pytest.approx(1.0)
    assert info["values"][0] == 0.0


def test_base_date_before_lookback_is_included():
    """基準日が30日より前でも、そこからの使用額を全部差し引く。"""
    daily = {"2026-06-15": 20.0, "2026-08-10": 5.0}
    info = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k", "ANTHROPIC_CREDIT_BASE": "2026-06-01:100"},
        fetcher=_fake_cost(daily),
    )
    assert info["spend_since_base_usd"] == pytest.approx(25.0)
    assert info["remaining_usd"] == pytest.approx(75.0)
    # 今月（8月）ぶんだけの集計は 5.0
    assert info["spend_month_usd"] == pytest.approx(5.0)


def test_no_admin_key_returns_guidance():
    info = ac.build_credit_info(today=TODAY, env={}, fetcher=_fake_cost({}))
    assert info["status"] == "no_key"
    assert "ANTHROPIC_ADMIN_KEY" in info["message"]
    assert info["remaining_usd"] is None


def test_key_without_base_shows_spend_only():
    info = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k"},
        fetcher=_fake_cost({"2026-08-11": 3.0}),
    )
    assert info["status"] == "ok"
    assert info["remaining_usd"] is None
    assert info["spend_yesterday_usd"] == pytest.approx(3.0)
    assert "ANTHROPIC_CREDIT_BASE" in info["message"]


def test_api_failure_does_not_raise():
    def _boom(admin_key, start_date, timeout=30):
        raise RuntimeError("401 Unauthorized")

    info = ac.build_credit_info(today=TODAY, env={"ANTHROPIC_ADMIN_KEY": "k"}, fetcher=_boom)
    assert info["status"] == "error"
    assert "401" in info["message"]
    assert info["remaining_usd"] is None


def test_no_days_left_when_no_usage():
    """使用実績ゼロなら「あと何日」は出さない（ゼロ除算しない）。"""
    info = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k", "ANTHROPIC_CREDIT_BASE": "2026-08-01:50"},
        fetcher=_fake_cost({}),
    )
    assert info["remaining_usd"] == pytest.approx(50.0)
    assert info["days_left"] is None
    assert info["empty_date"] is None


# ── メール本文への埋め込み ──────────────────────────────────────
def test_credit_section_renders():
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report

    ok = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k", "ANTHROPIC_CREDIT_BASE": "2026-08-01:100",
             "USD_JPY": "150"},
        fetcher=_fake_cost({f"2026-08-{d:02d}": 1.0 for d in range(1, 12)}),
    )
    html = send_report._credit_section(ok, 400)
    assert "クレジット残高" in html
    assert "cid:chart_credit" in html
    assert "¥13,350" in html          # 残高 $89 × 150
    assert "約89日" in html

    # 取得できなかった場合も落ちず、案内文が出る
    ng = ac.build_credit_info(today=TODAY, env={}, fetcher=_fake_cost({}))
    html_ng = send_report._credit_section(ng, 400)
    assert "取得できず" in html_ng
    assert "ANTHROPIC_ADMIN_KEY" in html_ng
    # 実データが無いときはグラフを出さない（案内文だけ）
    assert "cid:chart_credit" not in html_ng
    assert send_report.has_credit_chart(ok) is True
    assert send_report.has_credit_chart(ng) is False

    # グラフは例外なく描ける
    assert send_report.chart_credit(ok)[:4] == b"\x89PNG"
