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


def test_no_admin_key_and_no_estimate_returns_guidance():
    """実額も推定材料も無いときだけ、何も出せない旨を案内する。"""
    info = ac.build_credit_info(today=TODAY, env={}, fetcher=_fake_cost({}))
    assert info["status"] == "no_key"
    assert info["message"]
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
    # 基準残高の登録先は GitHub Secrets ではなくダッシュボードに変更した
    assert "ダッシュボード" in info["message"]


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


# ── オートリロード（自動チャージ）────────────────────────────────
# 経緯：2026-08 にオーナーが Console で「残高が $5 を下回ったら $100 追加」を
# 有効化した。単純な引き算のままだと入金ぶんが計算に入らず、残高がやがて
# マイナスへ際限なくズレていくため、日別にチャージを再現するようにした。
@pytest.mark.parametrize("raw,expected", [
    ("5:100", (5.0, 100.0)),
    (" $5 : $100 ", (5.0, 100.0)),
    ("5：100", (5.0, 100.0)),          # 全角コロン
    ("10:1,000", (10.0, 1000.0)),
])
def test_parse_auto_reload_ok(raw, expected):
    assert ac.parse_auto_reload(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "100", "5:abc", "5:0", "5:-100"])
def test_parse_auto_reload_ng(raw):
    """追加額が 0 以下だと残高が戻らず無限ループになるので無効扱いにする。"""
    assert ac.parse_auto_reload(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("300", 300.0), ("$300", 300.0), ("1,000", 1000.0),
    ("", None), (None, None), ("abc", None), ("0", None), ("-5", None),
])
def test_parse_spend_limit(raw, expected):
    assert ac.parse_spend_limit(raw) == expected


def test_auto_reload_tops_up_balance():
    """基準 $5.76 から毎日 $8 使っても、残高はマイナスにならずチャージされる。"""
    daily = {f"2026-08-{d:02d}": 8.0 for d in range(1, 12)}
    info = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k",
             "ANTHROPIC_CREDIT_BASE": "2026-08-01:5.76",
             "ANTHROPIC_AUTO_RELOAD": "5:100"},
        fetcher=_fake_cost(daily),
    )
    # 8/1〜8/11 で $88 使用。5.76 - 88 + 100×N が残高
    assert info["spend_since_base_usd"] == pytest.approx(88.0)
    assert info["reload_count"] == 1
    assert info["reload_total_usd"] == pytest.approx(100.0)
    assert info["remaining_usd"] == pytest.approx(17.76)
    assert info["remaining_usd"] > 0                # ← 以前はマイナスになっていた
    # オートリロードがある間は「枯渇」ではなく「次のチャージ日」を出す
    assert info["days_left"] is None and info["empty_date"] is None
    assert info["next_reload_days"] == 1            # (17.76-5)//8 = 1
    assert info["next_reload_date"] == "2026-08-13"


def test_auto_reload_handles_multiple_topups_in_one_day():
    """1日で追加額を超えて使っても、残高がしきい値以上に戻るまで繰り返す。"""
    info = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k",
             "ANTHROPIC_CREDIT_BASE": "2026-08-10:10",
             "ANTHROPIC_AUTO_RELOAD": "5:100"},
        fetcher=_fake_cost({"2026-08-10": 250.0}),
    )
    assert info["reload_count"] == 3                # 10-250 = -240 → +300
    assert info["remaining_usd"] == pytest.approx(60.0)


def test_without_auto_reload_behaviour_is_unchanged():
    """未設定なら従来どおり「引き算だけ」「枯渇予測あり」。"""
    daily = {f"2026-08-{d:02d}": 1.0 for d in range(1, 12)}
    info = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k", "ANTHROPIC_CREDIT_BASE": "2026-08-01:100"},
        fetcher=_fake_cost(daily),
    )
    assert info["reload_count"] is None
    assert info["remaining_usd"] == pytest.approx(89.0)
    assert info["days_left"] == 89
    assert info["next_reload_days"] is None


def test_spend_limit_projection_flags_risk():
    """月間支出上限に達するとAPIが止まるので、月末見込みが上限を超えるなら警告する。"""
    daily = {f"2026-08-{d:02d}": 10.0 for d in range(1, 12)}   # 今月 $110、7日平均 $10
    env = {"ANTHROPIC_ADMIN_KEY": "k", "ANTHROPIC_CREDIT_BASE": "2026-08-01:100",
           "ANTHROPIC_SPEND_LIMIT": "300"}
    info = ac.build_credit_info(today=TODAY, env=env, fetcher=_fake_cost(daily))
    assert info["spend_limit_usd"] == 300.0
    assert info["spend_month_pct"] == pytest.approx(36.7, abs=0.1)   # 110/300
    # 8/12〜8/31 の20日ぶんを上乗せ： 110 + 10×20 = 310
    assert info["projected_month_usd"] == pytest.approx(310.0)
    assert info["limit_risk"] is True

    # 上限が十分高ければ警告しない
    info2 = ac.build_credit_info(today=TODAY, fetcher=_fake_cost(daily),
                                 env={**env, "ANTHROPIC_SPEND_LIMIT": "1000"})
    assert info2["limit_risk"] is False

    # 上限にぶつかると残高があってもAPIが止まるので、届く手前（9割）で警告を出す
    near = ac.build_credit_info(today=TODAY, fetcher=_fake_cost(daily),
                                env={**env, "ANTHROPIC_SPEND_LIMIT": "340"})
    assert near["projected_month_usd"] == pytest.approx(310.0)   # 340 の 91%
    assert near["limit_risk"] is True
    far = ac.build_credit_info(today=TODAY, fetcher=_fake_cost(daily),
                               env={**env, "ANTHROPIC_SPEND_LIMIT": "360"})
    assert far["limit_risk"] is False                            # 360 の 86%


def test_spend_limit_unset_leaves_fields_empty():
    info = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k", "ANTHROPIC_CREDIT_BASE": "2026-08-01:100"},
        fetcher=_fake_cost({"2026-08-11": 1.0}),
    )
    assert info["spend_limit_usd"] is None
    assert info["projected_month_usd"] is None
    assert info["limit_risk"] is False


@pytest.mark.parametrize("day,expected", [
    (datetime.date(2026, 8, 12), datetime.date(2026, 8, 31)),
    (datetime.date(2026, 2, 3), datetime.date(2026, 2, 28)),
    (datetime.date(2026, 12, 1), datetime.date(2026, 12, 31)),
])
def test_month_end(day, expected):
    assert ac._month_end(day) == expected


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
    assert "残高" in html_ng
    # 実データが無いときはグラフを出さない（案内文だけ）
    assert "cid:chart_credit" not in html_ng
    assert send_report.has_credit_chart(ok) is True
    assert send_report.has_credit_chart(ng) is False

    # グラフは例外なく描ける
    assert send_report.chart_credit(ok)[:4] == b"\x89PNG"


def test_credit_section_shows_auto_reload_and_limit():
    """オートリロード中は「枯渇」ではなく「次の自動チャージ」と上限消化率を出す。"""
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report

    info = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k",
             "ANTHROPIC_CREDIT_BASE": "2026-08-01:5.76",
             "ANTHROPIC_AUTO_RELOAD": "5:100",
             "ANTHROPIC_SPEND_LIMIT": "200", "USD_JPY": "150"},
        fetcher=_fake_cost({f"2026-08-{d:02d}": 8.0 for d in range(1, 12)}),
    )
    html = send_report._credit_section(info, 400)
    assert "次の自動チャージ" in html
    assert "枯渇" not in html
    assert "$100.00" in html
    assert "上限 $200.00" in html
    # 今月 $88 ＋ 残り20日×$8 = $248 で上限 $200 を超えるので警告を出す
    assert "月末見込み $248.00" in html
    assert send_report.chart_credit(info)[:4] == b"\x89PNG"

    # 上限に余裕があるときは警告を出さない
    safe = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k",
             "ANTHROPIC_CREDIT_BASE": "2026-08-01:5.76",
             "ANTHROPIC_AUTO_RELOAD": "5:100", "ANTHROPIC_SPEND_LIMIT": "1000"},
        fetcher=_fake_cost({f"2026-08-{d:02d}": 8.0 for d in range(1, 12)}),
    )
    assert "月末見込み" not in send_report._credit_section(safe, 400)


# ── 実額が取れないときの「推定残高」（オーナー指示 2026-08）──────────
# 「クレジット残高が取得できない場合は予測でも良いので、だいたいあとどれくらい
#   残高があるか教えて」への対応。アプリ自身の解析回数から使用額を見積もる。
def _estimate(daily_jpy, base_date="2026-08-01", base_usd=100.0):
    return {"daily_jpy": daily_jpy, "base_date": base_date, "base_usd": base_usd,
            "cost_per_analysis_jpy": 4}


def test_estimates_balance_without_admin_key():
    """Admin APIキーが無くても、残高の目安が出ること。"""
    # 8/1〜8/11 の11日間、毎日 ¥1,550（＝$10）使った
    daily = {f"2026-08-{d:02d}": 1550 for d in range(1, 12)}
    info = ac.build_credit_info(today=TODAY, env={"USD_JPY": "155"},
                                estimate=_estimate(daily))
    assert info["status"] == "estimated"
    assert info["estimated"] is True
    assert info["spend_since_base_usd"] == pytest.approx(110.0)
    assert info["remaining_usd"] == pytest.approx(-10.0)
    assert info["spend_yesterday_usd"] == pytest.approx(10.0)
    assert "推定" in info["message"]


def test_estimate_gives_days_left():
    """残りが何日もつかの目安も出ること。"""
    daily = {f"2026-08-{d:02d}": 155 for d in range(1, 12)}   # 毎日 $1
    info = ac.build_credit_info(today=TODAY, env={"USD_JPY": "155"},
                                estimate=_estimate(daily, base_usd=100.0))
    assert info["remaining_usd"] == pytest.approx(89.0)
    assert info["days_left"] == 89
    assert info["empty_date"] == "2026-11-09"


def test_real_cost_wins_over_estimate():
    """Admin APIキーがあるときは実額を使う（推定で上書きしない）。"""
    info = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k", "USD_JPY": "155"},
        fetcher=_fake_cost({"2026-08-11": 2.0}),
        estimate=_estimate({"2026-08-11": 99999}),
    )
    assert info["status"] == "ok"
    assert info["estimated"] is False
    assert info["spend_yesterday_usd"] == pytest.approx(2.0)


def test_falls_back_to_estimate_when_api_fails():
    """実額の取得に失敗したら、推定に切り替えて残高を出し続けること。"""
    def _boom(admin_key, start_date, timeout=30):
        raise RuntimeError("401 Unauthorized")

    info = ac.build_credit_info(
        today=TODAY,
        env={"ANTHROPIC_ADMIN_KEY": "k", "USD_JPY": "155"},
        fetcher=_boom,
        estimate=_estimate({"2026-08-11": 1550}),
    )
    assert info["status"] == "estimated"
    assert info["remaining_usd"] is not None
    assert "401" in info["message"]      # 失敗した事実も残す


def test_dashboard_base_is_used_when_secret_is_absent():
    """基準残高はダッシュボード登録ぶんでも効くこと（Secrets必須にしない）。"""
    info = ac.build_credit_info(today=TODAY, env={"USD_JPY": "155"},
                                estimate=_estimate({"2026-08-11": 1550},
                                                   base_date="2026-08-10", base_usd=50.0))
    assert info["base_date"] == "2026-08-10"
    assert info["remaining_usd"] == pytest.approx(40.0)


def test_secret_base_wins_over_dashboard():
    info = ac.build_credit_info(
        today=TODAY,
        env={"USD_JPY": "155", "ANTHROPIC_CREDIT_BASE": "2026-08-10:70"},
        estimate=_estimate({"2026-08-11": 1550}, base_date="2026-08-10", base_usd=50.0),
    )
    assert info["base_usd"] == pytest.approx(70.0)


def test_estimated_section_uses_the_full_layout():
    """推定でも「取得できず」ではなく、実額と同じ体裁で残高を出すこと。"""
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "report"))
    import send_report

    info = ac.build_credit_info(today=TODAY, env={"USD_JPY": "155"},
                                estimate=_estimate({f"2026-08-{d:02d}": 155 for d in range(1, 12)}))
    html = send_report._credit_section(info, 400)
    assert "取得できず" not in html, "推定できるのに『取得できず』になっています"
    assert "クレジット残高（推定）" in html
    assert ">推定<" in html, "実額か推定かのバッジが出ていません"
    assert "¥13,795" in html          # 残高 $89 × 155
    assert "約89日" in html
