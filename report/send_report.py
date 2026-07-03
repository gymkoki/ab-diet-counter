#!/usr/bin/env python3
"""
ABダイエット Bカウンター デイリーレポート送信スクリプト
毎朝 8:00 JST に GitHub Actions から実行される。
"""
import os
import io
import json
import time
import base64
import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
try:
    import japanize_matplotlib  # noqa: F401 — 日本語フォント自動設定
except ImportError:
    pass

# ── 設定 ──────────────────────────────────────────────────────
APP_URL        = os.environ.get("APP_URL", "https://ab-diet-counter.onrender.com").rstrip("/")
ADMIN_USER     = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS     = os.environ.get("ADMIN_PASSWORD", "")

def _clean_secret(value: str) -> str:
    """コピペ時に混入しがちな空白・改行・全角スペース（&nbsp;由来）を除去する。"""
    for ch in (" ", " ", "\t", "\r", "\n"):
        value = value.replace(ch, "")
    return value

GMAIL_USER     = _clean_secret(os.environ.get("GMAIL_USER", ""))
GMAIL_PASS     = _clean_secret(os.environ.get("GMAIL_APP_PASSWORD", ""))
REPORT_TO      = os.environ.get("REPORT_TO", "rits.1159@gmail.com")

JST = datetime.timezone(datetime.timedelta(hours=9))

# カラーパレット（アプリのブランドカラーに準拠）
C_PRIMARY  = "#FF6B35"
C_INDIGO   = "#6366F1"
C_GREEN    = "#10B981"
C_AMBER    = "#F59E0B"
C_GRAY     = "#9CA3AF"


# ── データ取得 ─────────────────────────────────────────────────
def wake_up():
    """Render のスリープ対策：最初にピングして起こす。"""
    try:
        requests.get(f"{APP_URL}/ping", timeout=30)
        time.sleep(8)
    except Exception:
        pass


def fetch_data() -> dict:
    wake_up()
    for attempt in range(3):
        try:
            r = requests.get(
                f"{APP_URL}/api/admin/report-data",
                auth=(ADMIN_USER, ADMIN_PASS),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 2:
                raise
            print(f"Retry {attempt + 1}: {e}")
            time.sleep(10)


# ── グラフ生成 ─────────────────────────────────────────────────
def fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode()
    plt.close(fig)
    return b64


def chart_usage(data: dict) -> str:
    """日別 解析回数（棒）＋ アクティブユーザー数（折れ線）"""
    dates     = data["dates"]
    analyses  = data["daily_analyses_trend"]
    users     = data["daily_users_trend"]
    costs     = [round(a * data["cost_per_analysis"]) for a in analyses]

    x = range(len(dates))
    tick_idx = [i for i in x if i % 5 == 0]
    tick_lbl = [dates[i][5:] for i in tick_idx]

    fig, ax1 = plt.subplots(figsize=(10, 3.8))
    ax2 = ax1.twinx()

    bars = ax1.bar(x, analyses, color=C_INDIGO + "CC", label="Analyses", zorder=2)
    line, = ax2.plot(x, users, color=C_PRIMARY, marker="o", markersize=4,
                     linewidth=2.2, label="Active Users", zorder=3)

    ax1.set_xticks(tick_idx)
    ax1.set_xticklabels(tick_lbl, fontsize=9)
    ax1.set_ylabel("食事分析回数", fontsize=9, color=C_INDIGO)
    ax2.set_ylabel("利用ユーザー数", fontsize=9, color=C_PRIMARY)
    ax1.set_ylim(bottom=0)
    ax2.set_ylim(bottom=0)
    ax1.tick_params(axis="y", colors=C_INDIGO, labelsize=9)
    ax2.tick_params(axis="y", colors=C_PRIMARY, labelsize=9)

    # コストをツールチップ代わりに最後の棒だけ注釈
    total_cost = sum(costs)
    ax1.set_title(
        f"日別 食事分析回数 & 利用ユーザー数（直近30日） — 推定コスト合計 ¥{total_cost:,}",
        fontsize=11, fontweight="bold", pad=8,
    )

    handles = [bars, line]
    labels  = ["食事分析回数", "利用ユーザー数"]
    ax1.legend(handles, labels, loc="upper left", fontsize=9)

    for sp in ["top"]:
        ax1.spines[sp].set_visible(False)
        ax2.spines[sp].set_visible(False)
    ax1.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_b64(fig)


def chart_hourly(data: dict) -> str:
    """時間帯別 解析分布（棒グラフ）"""
    hourly = data["hourly"]
    x = range(24)

    fig, ax = plt.subplots(figsize=(10, 3.2))
    colors = [C_PRIMARY if h in (7, 8, 12, 13, 19, 20) else C_INDIGO + "99" for h in x]
    ax.bar(x, hourly, color=colors, zorder=2)
    ax.set_xticks(range(24))
    ax.set_xticklabels([f"{h}" for h in range(24)], fontsize=8)
    ax.set_xlabel("時 (JST)", fontsize=9)
    ax.set_ylabel("解析回数", fontsize=9)
    ax.set_title(f"時間帯別 解析分布（{data['report_date']}）", fontsize=11, fontweight="bold", pad=8)
    ax.set_ylim(bottom=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_b64(fig)


def chart_b_count(data: dict) -> str:
    """Bカウント推移：スパゲッティ（個人）＋ 集団平均折れ線"""
    dates  = data["dates"]
    avg    = data["b_avg_trend"]
    indivs = data["individual_b"]

    fig, ax = plt.subplots(figsize=(10, 4.2))

    cmap = plt.get_cmap("tab20")
    for i, user in enumerate(indivs):
        vals = user["values"]
        pts  = [(j, v) for j, v in enumerate(vals) if v is not None]
        if len(pts) < 2:
            continue
        xs, ys = zip(*pts)
        ax.plot(xs, ys, color=cmap(i % 20), alpha=0.30, linewidth=1.3,
                marker=".", markersize=4, zorder=2)

    avg_pts = [(j, v) for j, v in enumerate(avg) if v is not None]
    if avg_pts:
        xs, ys = zip(*avg_pts)
        ax.plot(xs, ys, color=C_PRIMARY, linewidth=2.8, zorder=4,
                label=f"集団平均 (直近: {ys[-1]:.1f}B)")
        ax.fill_between(xs, ys, alpha=0.12, color=C_PRIMARY, zorder=3)

    tick_idx = [i for i in range(len(dates)) if i % 5 == 0]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([dates[i][5:] for i in tick_idx], fontsize=9)
    ax.set_ylabel("Bカウント / 日", fontsize=9)
    ax.set_ylim(bottom=0)
    ax.set_title("Bカウント推移（直近30日）— 個人 + 集団平均",
                 fontsize=11, fontweight="bold", pad=8)
    ax.legend(fontsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_b64(fig)


def chart_weight(data: dict) -> str:
    """体重推移：スパゲッティ（個人）＋ 集団平均折れ線"""
    dates  = data["dates"]
    avg    = data["w_avg_trend"]
    indivs = data["individual_w"]

    fig, ax = plt.subplots(figsize=(10, 4.2))

    cmap = plt.get_cmap("tab20c")
    for i, user in enumerate(indivs):
        vals = user["values"]
        pts  = [(j, v) for j, v in enumerate(vals) if v is not None]
        if len(pts) < 2:
            continue
        xs, ys = zip(*pts)
        ax.plot(xs, ys, color=cmap(i % 20), alpha=0.30, linewidth=1.3,
                marker=".", markersize=4, zorder=2)

    avg_pts = [(j, v) for j, v in enumerate(avg) if v is not None]
    if avg_pts:
        xs, ys = zip(*avg_pts)
        ax.plot(xs, ys, color=C_GREEN, linewidth=2.8, zorder=4,
                label=f"集団平均 (直近: {ys[-1]:.1f}kg)")
        ax.fill_between(xs, ys, alpha=0.12, color=C_GREEN, zorder=3)

    tick_idx = [i for i in range(len(dates)) if i % 5 == 0]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([dates[i][5:] for i in tick_idx], fontsize=9)
    ax.set_ylabel("体重 (kg)", fontsize=9)
    ax.set_title("体重推移（直近30日）— 個人 + 集団平均",
                 fontsize=11, fontweight="bold", pad=8)
    ax.legend(fontsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_b64(fig)


# ── メール HTML 本文 ────────────────────────────────────────────
def _bar(val: int, total: int, color: str) -> str:
    pct = round(val / total * 100) if total else 0
    return (
        f'<div style="flex:1;height:8px;background:#E5E7EB;border-radius:4px;overflow:hidden">'
        f'<div style="width:{pct}%;height:100%;background:{color};border-radius:4px"></div></div>'
    )


def build_html(data: dict, charts: dict) -> str:
    rdate = data["report_date"]
    sc    = data["slot_counts"]
    sp    = data["slot_photo"]
    st    = data["slot_text"]
    total_slot = sum(sc.values())
    now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    slot_rows = ""
    for slot_id, emoji, label, color in [
        ("breakfast", "🌅", "朝食", C_PRIMARY),
        ("lunch",     "☀️", "昼食", C_AMBER),
        ("dinner",    "🌙", "夕食", C_INDIGO),
        ("snack",     "🍪", "間食", C_GREEN),
    ]:
        cnt = sc[slot_id]
        slot_rows += f"""
        <tr>
          <td style="padding:8px 12px;font-size:14px;color:#374151;font-weight:700;white-space:nowrap">{emoji} {label}</td>
          <td style="padding:8px 12px;font-size:16px;font-weight:900;color:{color};text-align:right">{cnt}</td>
          <td style="padding:8px 12px;font-size:12px;color:#6B7280">
            📸{sp[slot_id]} / 📝{st[slot_id]}
          </td>
          <td style="padding:8px 12px;width:200px">{_bar(cnt, total_slot, color)}</td>
        </tr>"""

    # 時間帯ピーク
    hourly = data["hourly"]
    peak_h = hourly.index(max(hourly)) if max(hourly) > 0 else 0
    peak_v = max(hourly)

    # 体重・Bカウント直近値
    b_avg_latest = next((v for v in reversed(data["b_avg_trend"]) if v is not None), None)
    w_avg_latest = next((v for v in reversed(data["w_avg_trend"]) if v is not None), None)
    b_latest_str = f"{b_avg_latest:.1f} B" if b_avg_latest is not None else "—"
    w_latest_str = f"{w_avg_latest:.1f} kg" if w_avg_latest is not None else "—"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{margin:0;padding:0;background:#F3F4F6;font-family:-apple-system,'Helvetica Neue',Arial,sans-serif}}
  .wrap{{max-width:680px;margin:0 auto;padding:20px}}
  .header{{background:linear-gradient(135deg,#FF6B35,#e55a25);color:#fff;border-radius:14px 14px 0 0;padding:24px 28px}}
  .header h1{{margin:0;font-size:20px;font-weight:900}}
  .header p{{margin:6px 0 0;font-size:13px;opacity:.88}}
  .body{{background:#fff;padding:24px 28px;border-radius:0 0 14px 14px}}
  .section{{margin-bottom:28px}}
  h2{{font-size:14px;font-weight:800;color:#374151;border-left:4px solid #FF6B35;padding-left:10px;margin:0 0 14px}}
  .kpi-row{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:4px}}
  .kpi{{flex:1;min-width:130px;background:#F9FAFB;border-radius:10px;padding:14px;text-align:center}}
  .kpi-lbl{{font-size:11px;color:#6B7280;font-weight:700;margin-bottom:4px}}
  .kpi-val{{font-size:26px;font-weight:900;color:#FF6B35;line-height:1}}
  .kpi-sub{{font-size:11px;color:#9CA3AF;margin-top:3px}}
  img.chart{{width:100%;max-width:640px;border-radius:10px;margin:6px 0;display:block}}
  .footer{{margin-top:20px;font-size:11px;color:#9CA3AF;text-align:center}}
  a{{color:#FF6B35}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>🏋️ AB Diet Bカウンター デイリーレポート</h1>
    <p>対象日: {rdate} &nbsp;|&nbsp; 配信: {now_str}</p>
  </div>
  <div class="body">

    <!-- ① 利用統計 -->
    <div class="section">
      <h2>📊 1. 利用統計 &amp; 食事記録（{rdate}）</h2>
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-lbl">デイリー利用者数</div>
          <div class="kpi-val">{data['daily_users']}</div>
          <div class="kpi-sub">ユーザー</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">食事分析回数</div>
          <div class="kpi-val">{data['daily_analyses']}</div>
          <div class="kpi-sub">（写真＋テキスト合計）</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">推定API費用</div>
          <div class="kpi-val">¥{data['est_cost_jpy']:,}</div>
          <div class="kpi-sub">¥{data['cost_per_analysis']}/回</div>
        </div>
      </div>

      <!-- 食事スロット別 -->
      <div style="margin-top:16px">
        <div style="font-size:12px;font-weight:700;color:#6B7280;margin-bottom:6px">
          食事スロット別記録回数（📸=写真 / 📝=テキスト）
        </div>
        <table style="width:100%;border-collapse:collapse">
          <tbody>{slot_rows}
            <tr style="border-top:2px solid #E5E7EB">
              <td style="padding:8px 12px;font-size:14px;font-weight:800;color:#111827">合計</td>
              <td style="padding:8px 12px;font-size:16px;font-weight:900;color:#111827;text-align:right">{total_slot}</td>
              <td colspan="2"></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ① 日別推移グラフ -->
    <div class="section">
      <h2>📈 日別 利用推移（直近30日）</h2>
      <img class="chart" src="data:image/png;base64,{charts['usage']}" alt="Usage Trend">
    </div>

    <!-- ② 体重・Bカウント推移 -->
    <div class="section">
      <h2>⚖️ 2. 体重 &amp; Bカウント推移</h2>
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-lbl">Bカウント集団平均（直近）</div>
          <div class="kpi-val" style="font-size:22px">{b_latest_str}</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">体重集団平均（直近）</div>
          <div class="kpi-val" style="font-size:22px;color:#10B981">{w_latest_str}</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">記録ユーザー数（B）</div>
          <div class="kpi-val" style="font-size:22px">{len(data['individual_b'])}</div>
          <div class="kpi-sub">（体重: {len(data['individual_w'])}名）</div>
        </div>
      </div>
      <img class="chart" src="data:image/png;base64,{charts['b_count']}" alt="B-Count Trend" style="margin-top:12px">
      <img class="chart" src="data:image/png;base64,{charts['weight']}" alt="Weight Trend" style="margin-top:6px">
    </div>

    <!-- ③ システム・運用状況 -->
    <div class="section">
      <h2>⚙️ 3. システム・運用状況</h2>

      <!-- 時間帯別分布 -->
      <div style="margin-bottom:14px">
        <div style="font-size:12px;font-weight:700;color:#6B7280;margin-bottom:6px">
          解析ピーク時間帯: <strong style="color:#FF6B35">{peak_h}:00〜{peak_h+1}:00</strong>（{peak_v}回）
        </div>
        <img class="chart" src="data:image/png;base64,{charts['hourly']}" alt="Hourly Distribution">
      </div>

      <!-- コスト情報 -->
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="border-bottom:1px solid #F3F4F6">
          <td style="padding:8px;color:#6B7280;font-weight:700">昨日の推定コスト</td>
          <td style="padding:8px;font-weight:800;color:#374151">¥{data['est_cost_jpy']:,}</td>
        </tr>
        <tr style="border-bottom:1px solid #F3F4F6">
          <td style="padding:8px;color:#6B7280;font-weight:700">1回あたり</td>
          <td style="padding:8px;font-weight:800;color:#374151">約 ¥{data['cost_per_analysis']}</td>
        </tr>
        <tr>
          <td style="padding:8px;color:#6B7280;font-weight:700">APIクレジット残高</td>
          <td style="padding:8px;color:#9CA3AF">
            <a href="https://console.anthropic.com/settings/billing" style="color:#FF6B35">
              Anthropic Console で確認 →
            </a>
          </td>
        </tr>
      </table>
    </div>

  </div>
  <div class="footer">
    <p>ABダイエット Bカウンター 自動レポート | 毎朝 8:00 JST 配信</p>
    <p>Generated: {now_str}</p>
  </div>
</div>
</body>
</html>"""


# ── メール送信 ──────────────────────────────────────────────────
def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"]    = GMAIL_USER
    msg["To"]      = REPORT_TO

    plain = f"ABダイエット Bカウンター デイリーレポート\nHTMLメールをご覧ください。\n生成時刻: {datetime.datetime.now(JST)}"
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(GMAIL_USER, GMAIL_PASS)
        smtp.sendmail(GMAIL_USER, [REPORT_TO], msg.as_bytes())

    print(f"Email sent → {REPORT_TO}")


# ── エントリーポイント ──────────────────────────────────────────
def main():
    print(f"[{datetime.datetime.now(JST).strftime('%H:%M:%S')}] Fetching data from {APP_URL}...")
    data = fetch_data()
    print(f"  report_date={data['report_date']}, daily_users={data['daily_users']}, "
          f"daily_analyses={data['daily_analyses']}")

    print("Generating charts...")
    charts = {
        "usage":   chart_usage(data),
        "hourly":  chart_hourly(data),
        "b_count": chart_b_count(data),
        "weight":  chart_weight(data),
    }

    print("Building HTML email...")
    rdate   = data["report_date"]
    subject = f"[ABダイエット] デイリーレポート {rdate}"
    html    = build_html(data, charts)

    if GMAIL_USER and GMAIL_PASS:
        print("Sending email...")
        send_email(subject, html)
    else:
        out_path = f"report_{rdate}.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Email config missing — saved to {out_path}")


if __name__ == "__main__":
    main()
