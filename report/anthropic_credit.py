#!/usr/bin/env python3
"""Claude API のコスト／クレジット残高情報を取得する。

【重要な前提】
Anthropic には「クレジット残高そのものを返すAPI」は存在しない（2026-08 時点）。
残高は Console → Settings → Billing でしか見られない。

そこで本モジュールは次の2段構えで「残りいくらか」を出す：

  1. Admin API の Cost API（GET /v1/organizations/cost_report）で
     "実際に使った金額（USD）" を日別に取得する。
     → ANTHROPIC_ADMIN_KEY（`sk-ant-admin01-...`）が必要。
        Console → Settings → Admin keys で発行する。通常のAPIキーでは使えない。

  2. オーナーが控えた「基準日の残高」（ANTHROPIC_CREDIT_BASE）から
     基準日以降の実使用額を差し引いて、現在の残高を算出する。
     → 例: ANTHROPIC_CREDIT_BASE = "2026-08-01:100"
        （2026-08-01 時点で Console の残高が $100 だった、の意味）

環境変数：
  ANTHROPIC_ADMIN_KEY   … Admin APIキー（未設定なら残高セクションは案内文だけになる）
  ANTHROPIC_CREDIT_BASE … "YYYY-MM-DD:USD金額"（未設定なら使用額のみ表示）
  USD_JPY               … 円換算レート（既定 155）
"""
import datetime
import os

API_BASE = "https://api.anthropic.com"
COST_ENDPOINT = f"{API_BASE}/v1/organizations/cost_report"
CONSOLE_BILLING_URL = "https://console.anthropic.com/settings/billing"

# Cost API の amount は「通貨の最小単位（USDならセント）の10進文字列」で返る。
# 例: "123.45" (USD) = $1.2345
#   https://platform.claude.com/docs/en/api/admin-api/usage-cost/get-cost-report
AMOUNT_UNITS_PER_USD = 100.0

# 残高を出せない場合でも直近30日ぶんの実使用額は出したいので、最低この日数はさかのぼる
MIN_LOOKBACK_DAYS = 30
# 1d バケットは1リクエスト最大31件。念のためページングは上限を設けて回す
MAX_PAGES = 24


def parse_credit_base(raw):
    """"YYYY-MM-DD:100" → (date(2026,8,1), 100.0)。不正なら None。

    "YYYY-MM-DD:$100.50" や "YYYY-MM-DD : 100" のような表記ゆれも受け付ける。
    """
    if not raw:
        return None
    text = str(raw).strip().replace("＄", "$").replace("：", ":")
    if ":" not in text:
        return None
    date_part, _, amount_part = text.partition(":")
    try:
        base_date = datetime.date.fromisoformat(date_part.strip())
    except ValueError:
        return None
    amount_part = amount_part.strip().lstrip("$").replace(",", "").replace(" ", "")
    try:
        amount = float(amount_part)
    except ValueError:
        return None
    return base_date, amount


def _amount_to_usd(amount) -> float:
    try:
        return float(amount) / AMOUNT_UNITS_PER_USD
    except (TypeError, ValueError):
        return 0.0


def fetch_cost_by_day(admin_key: str, start_date: datetime.date, timeout: int = 30) -> dict:
    """Cost API を叩いて {"YYYY-MM-DD": USD} を返す（UTC日別）。

    ネットワーク／認証エラーはそのまま例外として上げる（呼び出し側で握る）。
    """
    import requests  # 遅延importにしてテスト時の依存を減らす

    params = {
        "starting_at": f"{start_date.isoformat()}T00:00:00Z",
        "bucket_width": "1d",
        "limit": 31,
    }
    headers = {
        "x-api-key": admin_key,
        "anthropic-version": "2023-06-01",
        "User-Agent": "ABDietCounter-DailyReport/1.0",
    }

    by_day = {}
    page = None
    for _ in range(MAX_PAGES):
        q = dict(params)
        if page:
            q["page"] = page
        r = requests.get(COST_ENDPOINT, params=q, headers=headers, timeout=timeout)
        r.raise_for_status()
        payload = r.json()
        for bucket in payload.get("data") or []:
            day = str(bucket.get("starting_at") or "")[:10]
            if not day:
                continue
            total = sum(_amount_to_usd(item.get("amount"))
                        for item in (bucket.get("results") or []))
            by_day[day] = by_day.get(day, 0.0) + total
        if not payload.get("has_more"):
            break
        page = payload.get("next_page")
        if not page:
            break
    return by_day


def _sum_from(by_day: dict, first_day: datetime.date) -> float:
    key = first_day.isoformat()
    return sum(v for d, v in by_day.items() if d >= key)


def build_credit_info(today=None, env=None, fetcher=None) -> dict:
    """デイリーレポートに載せるクレジット情報を組み立てる。

    fetcher は差し替え可能（テスト用）。シグネチャは fetch_cost_by_day と同じ。
    戻り値の status は "ok" / "no_key" / "error"。
    """
    env = os.environ if env is None else env
    fetcher = fetch_cost_by_day if fetcher is None else fetcher
    today = datetime.datetime.now(datetime.timezone.utc).date() if today is None else today

    try:
        usd_jpy = float(env.get("USD_JPY") or 155)
    except ValueError:
        usd_jpy = 155.0

    base = parse_credit_base(env.get("ANTHROPIC_CREDIT_BASE"))
    info = {
        "status": "no_key",
        "message": "",
        "usd_jpy": usd_jpy,
        "console_url": CONSOLE_BILLING_URL,
        "daily": {},
        "dates": [],
        "values": [],
        "spend_yesterday_usd": None,
        "spend_month_usd": None,
        "spend_7d_avg_usd": None,
        "base_date": base[0].isoformat() if base else None,
        "base_usd": base[1] if base else None,
        "spend_since_base_usd": None,
        "remaining_usd": None,
        "days_left": None,
        "empty_date": None,
    }

    admin_key = (env.get("ANTHROPIC_ADMIN_KEY") or "").strip()
    if not admin_key:
        info["message"] = (
            "Admin APIキーが未設定のため、実際の使用額を取得できませんでした。"
            "Console → Settings → Admin keys でキーを発行し、GitHub Secrets に "
            "ANTHROPIC_ADMIN_KEY として登録すると、ここに実額が表示されます。"
        )
        return info

    lookback_start = today - datetime.timedelta(days=MIN_LOOKBACK_DAYS)
    start_date = min(base[0], lookback_start) if base else lookback_start

    try:
        by_day = fetcher(admin_key, start_date)
    except Exception as e:  # noqa: BLE001 — レポート全体を落とさない
        info["status"] = "error"
        info["message"] = f"Cost API の取得に失敗しました（{type(e).__name__}: {e}）。"
        return info

    info["status"] = "ok"
    info["daily"] = by_day

    # 直近30日ぶんを日付順に並べる（データが無い日は 0 として埋める）
    dates, values = [], []
    for i in range(MIN_LOOKBACK_DAYS, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        dates.append(d)
        values.append(round(by_day.get(d, 0.0), 6))
    info["dates"] = dates
    info["values"] = values

    yesterday = today - datetime.timedelta(days=1)
    info["spend_yesterday_usd"] = round(by_day.get(yesterday.isoformat(), 0.0), 4)
    info["spend_month_usd"] = round(_sum_from(by_day, today.replace(day=1)), 4)

    # 直近7日（昨日まで）の平均。使い切り予測に使う
    week = [by_day.get((yesterday - datetime.timedelta(days=i)).isoformat(), 0.0)
            for i in range(7)]
    avg7 = sum(week) / 7.0
    info["spend_7d_avg_usd"] = round(avg7, 4)

    if base:
        base_date, base_usd = base
        spent = _sum_from(by_day, base_date)
        info["spend_since_base_usd"] = round(spent, 4)
        remaining = base_usd - spent
        info["remaining_usd"] = round(remaining, 2)
        if avg7 > 0 and remaining > 0:
            days_left = int(remaining // avg7)
            info["days_left"] = days_left
            info["empty_date"] = (today + datetime.timedelta(days=days_left)).isoformat()
    else:
        info["message"] = (
            "残高の基準値が未設定です。Console の Billing で現在の残高を確認し、"
            'GitHub Secrets に ANTHROPIC_CREDIT_BASE = "YYYY-MM-DD:残高USD"'
            "（例: 2026-08-01:100）を登録すると、残高と使い切り予測が表示されます。"
        )
    return info
