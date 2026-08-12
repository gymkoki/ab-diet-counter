#!/usr/bin/env python3
"""
ABダイエット デイリーレポート送信スクリプト
毎朝 8:00 JST に GitHub Actions から実行される。
"""
import os
import io
import json
import time
import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
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

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anthropic_credit import build_credit_info  # noqa: E402

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
# ${{ secrets.REPORT_TO }} が未設定でも workflow 側で空文字の環境変数として渡されるため、
# os.environ.get の default 引数だけでは効かない。空文字化・空白混入の両方をここで吸収する。
REPORT_TO      = _clean_secret(os.environ.get("REPORT_TO", "")) or "reallgym.tokyo@gmail.com"

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
def fig_to_png(fig) -> bytes:
    """図をPNGバイト列にする。メール本文にはbase64で直接埋め込まず、
    cid参照の添付画像として送る（Gmailの本文102KB自動クリッピング対策）。"""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    return buf.getvalue()


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
    ax1.set_xticklabels(tick_lbl, fontsize=13)
    ax1.set_ylabel("食事分析回数", fontsize=13, color=C_INDIGO)
    ax2.set_ylabel("利用ユーザー数", fontsize=13, color=C_PRIMARY)
    ax1.set_ylim(bottom=0)
    ax2.set_ylim(bottom=0)
    ax1.tick_params(axis="y", colors=C_INDIGO, labelsize=13)
    ax2.tick_params(axis="y", colors=C_PRIMARY, labelsize=13)

    # コストをツールチップ代わりに最後の棒だけ注釈
    total_cost = sum(costs)
    ax1.set_title(
        f"日別 食事分析回数 & 利用ユーザー数（直近30日） — 推定コスト合計 ¥{total_cost:,}",
        fontsize=15, fontweight="bold", pad=8,
    )

    handles = [bars, line]
    labels  = ["食事分析回数", "利用ユーザー数"]
    ax1.legend(handles, labels, loc="upper left", fontsize=13)

    for sp in ["top"]:
        ax1.spines[sp].set_visible(False)
        ax2.spines[sp].set_visible(False)
    ax1.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_png(fig)


def chart_hourly(data: dict) -> str:
    """時間帯別 解析分布（棒グラフ）"""
    hourly = data["hourly"]
    x = range(24)

    fig, ax = plt.subplots(figsize=(10, 3.2))
    colors = [C_PRIMARY if h in (7, 8, 12, 13, 19, 20) else C_INDIGO + "99" for h in x]
    ax.bar(x, hourly, color=colors, zorder=2)
    ax.set_xticks(range(24))
    ax.set_xticklabels([f"{h}" for h in range(24)], fontsize=12)
    ax.set_xlabel("時 (JST)", fontsize=13)
    ax.set_ylabel("解析回数", fontsize=13)
    ax.set_title(f"時間帯別 解析分布（{data['report_date']}）", fontsize=15, fontweight="bold", pad=8)
    ax.set_ylim(bottom=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_png(fig)


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
    ax.set_xticklabels([dates[i][5:] for i in tick_idx], fontsize=13)
    ax.set_ylabel("Bカウント / 日", fontsize=13)
    ax.set_ylim(bottom=0)
    ax.set_title("Bカウント推移（直近30日）— 個人 + 集団平均",
                 fontsize=15, fontweight="bold", pad=8)
    ax.legend(fontsize=13)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_png(fig)


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
    ax.set_xticklabels([dates[i][5:] for i in tick_idx], fontsize=13)
    ax.set_ylabel("体重 (kg)", fontsize=13)
    ax.set_title("体重推移（直近30日）— 個人 + 集団平均",
                 fontsize=15, fontweight="bold", pad=8)
    ax.legend(fontsize=13)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_png(fig)


def chart_weight_loss(data: dict) -> str:
    """減量進捗（初回記録比）：スパゲッティ（個人）＋ 集団平均折れ線
    値は「初回体重 − その日の体重」（プラス＝減量、マイナス＝増量）を表す。"""
    dates  = data["dates"]
    avg    = data["loss_avg_trend"]
    indivs = data["individual_loss"]

    fig, ax = plt.subplots(figsize=(10, 4.2))

    cmap = plt.get_cmap("tab20b")
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
        latest = ys[-1]
        latest_lbl = f"{latest:.1f}kg減量" if latest >= 0 else f"{-latest:.1f}kg増加"
        ax.plot(xs, ys, color=C_AMBER, linewidth=2.8, zorder=4,
                label=f"集団平均 (直近: {latest_lbl})")
        ax.fill_between(xs, ys, alpha=0.12, color=C_AMBER, zorder=3)

    ax.axhline(0, color=C_GRAY, linewidth=1, zorder=1)
    tick_idx = [i for i in range(len(dates)) if i % 5 == 0]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([dates[i][5:] for i in tick_idx], fontsize=13)
    ax.set_ylabel("初回記録からの減量 (kg)", fontsize=13)
    ax.set_title("減量進捗（初回体重比）推移 — 個人 + 集団平均",
                 fontsize=15, fontweight="bold", pad=8)
    ax.legend(fontsize=13)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_png(fig)


def chart_exercise(data: dict) -> str:
    """運動によるBカウント消費：集団平均推移（棒グラフ）"""
    dates = data["dates"]
    avg   = data["ex_avg_trend"]

    fig, ax = plt.subplots(figsize=(10, 3.2))

    x = [j for j, v in enumerate(avg) if v is not None]
    y = [v for v in avg if v is not None]
    ax.bar(x, y, color=C_INDIGO + "CC", zorder=2)

    tick_idx = [i for i in range(len(dates)) if i % 5 == 0]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([dates[i][5:] for i in tick_idx], fontsize=13)
    ax.set_ylabel("消費Bカウント / 日", fontsize=13)
    ax.set_ylim(bottom=0)
    latest = next((v for v in reversed(avg) if v is not None), None)
    title_suffix = f"（直近: {latest:.1f}B）" if latest is not None else ""
    ax.set_title(f"運動によるBカウント消費 集団平均（直近30日）{title_suffix}",
                 fontsize=15, fontweight="bold", pad=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_png(fig)


def _axes_note(ax, msg: str):
    """データがまだ無いとき、グラフの代わりに案内文を表示する。"""
    ax.text(0.5, 0.5, msg, ha="center", va="center", fontsize=13,
            color=C_GRAY, transform=ax.transAxes, linespacing=1.8)
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)


def chart_cut_corr(data: dict) -> bytes:
    """減量希望者：平均Bカウント（左軸）× 平均減量幅（右軸）の推移。
    Bカウントを抑えられている時期に減量が進んでいるか、相関を目視できる。"""
    cc     = data.get("cut_corr") or {}
    dates  = data["dates"]
    b_tr   = cc.get("b_avg_trend") or []
    l_tr   = cc.get("loss_avg_trend") or []
    users  = cc.get("users", 0)

    fig, ax1 = plt.subplots(figsize=(10, 4.2))
    b_pts = [(i, v) for i, v in enumerate(b_tr) if v is not None]
    l_pts = [(i, v) for i, v in enumerate(l_tr) if v is not None]

    if users == 0 or (not b_pts and not l_pts):
        ax1.set_title("減量希望者：平均Bカウント × 平均減量の推移",
                      fontsize=15, fontweight="bold", pad=8)
        _axes_note(ax1, "減量希望者のデータを収集中です。\n会員がアプリで食事解析または設定保存をすると\n目標（減量/維持）が自動で記録されます。")
        fig.tight_layout()
        return fig_to_png(fig)

    ax2 = ax1.twinx()
    if b_pts:
        xs, ys = zip(*b_pts)
        ax1.plot(xs, ys, color=C_PRIMARY, linewidth=2.4, marker="o", markersize=4,
                 label=f"平均Bカウント (直近: {ys[-1]:.1f}B)", zorder=3)
    if l_pts:
        xs, ys = zip(*l_pts)
        latest = ys[-1]
        lbl = f"{latest:.1f}kg減量" if latest >= 0 else f"{-latest:.1f}kg増加"
        ax2.plot(xs, ys, color=C_GREEN, linewidth=2.4, marker="s", markersize=4,
                 label=f"平均減量幅 (直近: {lbl})", zorder=3)
        ax2.fill_between(xs, ys, alpha=0.10, color=C_GREEN, zorder=2)
    ax2.axhline(0, color=C_GRAY, linewidth=1, zorder=1)

    tick_idx = [i for i in range(len(dates)) if i % 5 == 0]
    ax1.set_xticks(tick_idx)
    ax1.set_xticklabels([dates[i][5:] for i in tick_idx], fontsize=13)
    ax1.set_ylabel("平均Bカウント / 日", fontsize=13, color=C_PRIMARY)
    ax2.set_ylabel("初回体重からの平均減量 (kg)", fontsize=13, color=C_GREEN)
    ax1.set_ylim(bottom=0)
    ax1.tick_params(axis="y", colors=C_PRIMARY, labelsize=13)
    ax2.tick_params(axis="y", colors=C_GREEN, labelsize=13)
    ax1.set_title(f"減量希望者（{users}名）：平均Bカウント × 平均減量の推移（直近30日）",
                  fontsize=15, fontweight="bold", pad=8)
    h1, lb1 = ax1.get_legend_handles_labels()
    h2, lb2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, lb1 + lb2, loc="upper left", fontsize=12)
    for sp in ["top"]:
        ax1.spines[sp].set_visible(False)
        ax2.spines[sp].set_visible(False)
    ax1.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_png(fig)


def chart_nutrition(data: dict) -> bytes:
    """栄養素（タンパク質・野菜・果物）の平均摂取量推移（1人1日あたり・g）"""
    nut   = data.get("nutrition") or {}
    dates = data["dates"]
    series = [
        ("タンパク質", nut.get("protein_avg_trend") or [], C_PRIMARY, "o"),
        ("野菜",       nut.get("veg_avg_trend") or [],     C_GREEN,   "s"),
        ("果物",       nut.get("fruit_avg_trend") or [],   C_AMBER,   "^"),
    ]

    fig, ax = plt.subplots(figsize=(10, 4.2))
    plotted = False
    for name, tr, color, marker in series:
        pts = [(i, v) for i, v in enumerate(tr) if v is not None]
        if not pts:
            continue
        plotted = True
        xs, ys = zip(*pts)
        ax.plot(xs, ys, color=color, linewidth=2.2, marker=marker, markersize=4,
                label=f"{name} (直近: {ys[-1]:.0f}g)", zorder=3)

    if not plotted:
        ax.set_title("栄養素の平均摂取量推移（1人1日あたり）", fontsize=15, fontweight="bold", pad=8)
        _axes_note(ax, "栄養素データを収集中です。\n会員の食事解析が貯まると表示されます。")
        fig.tight_layout()
        return fig_to_png(fig)

    # 野菜の目標350gの目安線（上に少し余白を取ってラベルが切れないようにする）
    data_max = max((v for tr in (nut.get("protein_avg_trend") or [], nut.get("veg_avg_trend") or [], nut.get("fruit_avg_trend") or [])
                    for v in tr if v is not None), default=0)
    ax.set_ylim(0, max(350, data_max) * 1.15)
    ax.axhline(350, color=C_GREEN, linewidth=1.2, linestyle="--", alpha=0.6, zorder=1)
    ax.text(len(dates) - 1, 356, "野菜目標 350g", fontsize=11, color=C_GREEN, alpha=0.9, ha="right")

    tick_idx = [i for i in range(len(dates)) if i % 5 == 0]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([dates[i][5:] for i in tick_idx], fontsize=13)
    ax.set_ylabel("平均摂取量 (g / 人・日)", fontsize=13)
    ax.set_title("栄養素の平均摂取量推移（直近30日・1人1日あたり）",
                 fontsize=15, fontweight="bold", pad=8)
    ax.legend(fontsize=12, loc="upper left")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_png(fig)


def chart_goal_compare(data: dict) -> bytes:
    """目的別比較：減量希望 vs 体重維持のBカウント・タンパク質・野菜の平均"""
    gc = data.get("goal_compare") or {}
    groups = [("cut", "減量希望", C_PRIMARY), ("maintain", "体重維持", C_INDIGO)]
    active = [(k, lbl, c) for k, lbl, c in groups if (gc.get(k) or {}).get("users", 0) > 0]

    fig, (axb, axn) = plt.subplots(1, 2, figsize=(10, 3.8), gridspec_kw={"width_ratios": [1, 1.6]})

    if not active:
        fig.suptitle("目的別比較（減量希望 vs 体重維持）", fontsize=15, fontweight="bold")
        _axes_note(axb, "目的別データを収集中です。")
        _axes_note(axn, "会員がアプリを利用すると\n目標（減量/維持）が自動で記録されます。")
        fig.tight_layout()
        return fig_to_png(fig)

    # 左：平均Bカウント
    xs = np.arange(len(active))
    b_vals = [(gc[k].get("avg_b") or 0) for k, _, _ in active]
    bars = axb.bar(xs, b_vals, width=0.55, color=[c for _, _, c in active], zorder=2)
    for x, v in zip(xs, b_vals):
        axb.text(x, v, f"{v:.1f}", ha="center", va="bottom", fontsize=13, fontweight="bold")
    axb.set_xticks(xs)
    axb.set_xticklabels([f"{lbl}\n({gc[k]['users']}名)" for k, lbl, _ in active], fontsize=12)
    axb.set_ylabel("平均Bカウント / 日", fontsize=12)
    axb.set_title("Bカウント", fontsize=13, fontweight="bold")
    axb.spines["top"].set_visible(False)
    axb.spines["right"].set_visible(False)
    axb.grid(axis="y", alpha=0.25, zorder=0)

    # 右：タンパク質・野菜（g）
    metrics = [("avg_protein", "タンパク質"), ("avg_veg", "野菜")]
    width = 0.34
    for gi, (k, lbl, color) in enumerate(active):
        vals = [(gc[k].get(m) or 0) for m, _ in metrics]
        pos  = np.arange(len(metrics)) + (gi - (len(active) - 1) / 2) * width
        axn.bar(pos, vals, width=width, color=color, label=lbl, zorder=2)
        for x, v in zip(pos, vals):
            axn.text(x, v, f"{v:.0f}", ha="center", va="bottom", fontsize=12, fontweight="bold")
    axn.set_xticks(np.arange(len(metrics)))
    axn.set_xticklabels([lbl for _, lbl in metrics], fontsize=12)
    axn.set_ylabel("平均摂取量 (g / 人・日)", fontsize=12)
    axn.set_title("タンパク質・野菜", fontsize=13, fontweight="bold")
    axn.legend(fontsize=11)
    axn.spines["top"].set_visible(False)
    axn.spines["right"].set_visible(False)
    axn.grid(axis="y", alpha=0.25, zorder=0)

    fig.suptitle("目的別比較（直近30日・1人1日あたりの平均）", fontsize=15, fontweight="bold")
    fig.tight_layout()
    return fig_to_png(fig)


# ── メール HTML 本文 ────────────────────────────────────────────
# 画像は cid:chart_usage / cid:chart_hourly / cid:chart_b_count /
# cid:chart_weight / cid:chart_weight_loss / cid:chart_exercise / cid:chart_credit で参照する（send_email() が
# main() で生成した charts dict のキーと同名の Content-ID を付けて添付する）。
def has_credit_chart(credit: dict) -> bool:
    """コストの実データが取れたときだけグラフを出す（取れないときは案内文だけ）。"""
    return bool(credit) and credit.get("status") == "ok" and bool(credit.get("dates"))


def chart_credit(credit: dict) -> bytes:
    """Claude API の日別実コスト（円換算）。残高が分かっていれば使い切り予測も併記。"""
    fig, ax = plt.subplots(figsize=(10, 3.2))

    dates = credit.get("dates") or []
    values = credit.get("values") or []
    rate = credit.get("usd_jpy") or 155.0
    yen = [v * rate for v in values]
    x = range(len(dates))
    ax.bar(x, yen, color=C_INDIGO + "CC", zorder=2)

    tick_idx = [i for i in x if i % 5 == 0]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([dates[i][5:] for i in tick_idx], fontsize=13)
    ax.set_ylabel("APIコスト (円/日)", fontsize=13)
    ax.set_ylim(bottom=0)

    remaining = credit.get("remaining_usd")
    if remaining is not None:
        suffix = f" — 残高 ¥{round(remaining * rate):,}"
        if credit.get("days_left") is not None:
            suffix += f"（あと約{credit['days_left']}日）"
    else:
        suffix = f" — 今月の使用額 ¥{round((credit.get('spend_month_usd') or 0) * rate):,}"
    ax.set_title(f"Claude API 日別コスト（直近30日）{suffix}",
                 fontsize=15, fontweight="bold", pad=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_png(fig)


def _credit_section(credit: dict, est_cost_jpy: int) -> str:
    """💳 Claude API クレジット状況セクション。

    Anthropic に残高を返すAPIは無いため、
    「Cost API の実使用額」＋「オーナーが控えた基準残高」から残りを算出している。
    """
    rate = credit.get("usd_jpy") or 155.0

    def _yen(usd):
        return f"¥{round(usd * rate):,}" if usd is not None else "—"

    def _usd(usd):
        return f"${usd:,.2f}" if usd is not None else "—"

    status = credit.get("status")
    note = ""

    if status != "ok":
        # 実額が取れないときは、アプリ側の解析回数ベースの推定値だけ出す
        head = f"""
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-lbl">昨日の推定コスト</div>
          <div class="kpi-val" style="font-size:22px">¥{est_cost_jpy:,}</div>
          <div class="kpi-sub">解析回数からの概算</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">クレジット残高</div>
          <div class="kpi-val" style="font-size:20px;color:#9CA3AF">取得できず</div>
          <div class="kpi-sub"><a href="{credit.get('console_url')}">Console で確認 →</a></div>
        </div>
      </div>"""
        note = credit.get("message") or ""
    else:
        remaining = credit.get("remaining_usd")
        if remaining is None:
            rem_val, rem_sub, rem_color = "未設定", "基準残高の登録が必要", "#9CA3AF"
        else:
            rem_val = _yen(remaining)
            rem_sub = f"{_usd(remaining)}（{credit.get('base_date')} 時点 {_usd(credit.get('base_usd'))} 基準）"
            rem_color = "#10B981" if remaining > (credit.get("spend_7d_avg_usd") or 0) * 14 else "#EF4444"

        if credit.get("days_left") is not None:
            days_val = f"約{credit['days_left']}日"
            days_sub = f"この使用ペースだと {credit.get('empty_date')} 頃に枯渇"
        else:
            days_val, days_sub = "—", "残高または使用実績が不足"

        head = f"""
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-lbl">💳 クレジット残高（推定）</div>
          <div class="kpi-val" style="font-size:24px;color:{rem_color}">{rem_val}</div>
          <div class="kpi-sub">{rem_sub}</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">⏳ 残り日数の目安</div>
          <div class="kpi-val" style="font-size:22px">{days_val}</div>
          <div class="kpi-sub">{days_sub}</div>
        </div>
      </div>
      <div class="kpi-row" style="margin-top:10px">
        <div class="kpi">
          <div class="kpi-lbl">昨日の実コスト</div>
          <div class="kpi-val" style="font-size:20px">{_yen(credit.get('spend_yesterday_usd'))}</div>
          <div class="kpi-sub">{_usd(credit.get('spend_yesterday_usd'))}</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">今月の実コスト</div>
          <div class="kpi-val" style="font-size:20px">{_yen(credit.get('spend_month_usd'))}</div>
          <div class="kpi-sub">{_usd(credit.get('spend_month_usd'))}</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">1日あたり平均</div>
          <div class="kpi-val" style="font-size:20px">{_yen(credit.get('spend_7d_avg_usd'))}</div>
          <div class="kpi-sub">直近7日平均</div>
        </div>
      </div>"""
        note = credit.get("message") or ""

    note_html = ""
    if note:
        note_html = f"""
      <div style="margin-top:10px;background:#FFFBEB;border:1px solid #FDE68A;border-radius:8px;
                  padding:10px 12px;font-size:12px;color:#92400E;line-height:1.7">{note}</div>"""

    chart_html = ""
    if has_credit_chart(credit):
        chart_html = '\n      <img class="chart" src="cid:chart_credit" alt="API Credit" style="margin-top:12px">'

    return f"""
    <div class="section">
      <h2>💳 6. Claude API クレジット状況</h2>{head}{chart_html}{note_html}
      <div style="font-size:11px;color:#9CA3AF;margin-top:6px">
        ※ 実コストは Anthropic の Cost API（実測値）。Anthropic には残高を返すAPIが無いため、
        残高は「基準日の残高 − 基準日以降の実使用額」で算出した推定値です。
        正確な残高は <a href="{credit.get('console_url')}">Anthropic Console</a> で確認できます。
        円換算レート: $1 = ¥{rate:.0f}
      </div>
    </div>"""


def _coach_section(advice) -> str:
    """AI減量コーチの提案セクション（メール最上部）。提案が無い日は出さない。"""
    if not advice:
        return ""
    esc = (advice.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return f"""
    <div class="section" style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:12px;padding:16px 18px">
      <h2 style="border-left-color:#EA580C">🎯 AI減量コーチ｜今日の提案（1か月 −1kg 目標）</h2>
      <div style="font-size:13px;color:#374151;line-height:1.9;white-space:pre-wrap">{esc}</div>
    </div>
    """


def fetch_coach_advice():
    """AI減量コーチの「今日の提案」を取得する（サーバー側で生成・日次キャッシュ）。
    失敗してもレポート本体は送る（提案セクションだけ省略）。"""
    for attempt in range(2):
        try:
            r = requests.get(
                f"{APP_URL}/api/admin/coach-advice",
                auth=(ADMIN_USER, ADMIN_PASS),
                timeout=150,   # AI生成に時間がかかることがある
            )
            r.raise_for_status()
            d = r.json()
            advice = (d.get("advice") or "").strip()
            return advice or None
        except Exception as e:
            print(f"coach-advice retry {attempt + 1}: {e}")
            time.sleep(10)
    return None


def build_html(data: dict, coach_advice=None, credit=None) -> str:
    rdate = data["report_date"]
    meal  = data["meal_summary"]
    now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    # 時間帯ピーク
    hourly = data["hourly"]
    peak_h = hourly.index(max(hourly)) if max(hourly) > 0 else 0
    peak_v = max(hourly)

    # 体重・Bカウント直近値
    b_avg_latest = next((v for v in reversed(data["b_avg_trend"]) if v is not None), None)
    w_avg_latest = next((v for v in reversed(data["w_avg_trend"]) if v is not None), None)
    b_latest_str = f"{b_avg_latest:.1f} B" if b_avg_latest is not None else "—"
    w_latest_str = f"{w_avg_latest:.1f} kg" if w_avg_latest is not None else "—"

    # 減量希望者の平均減量実績（初回記録 vs 最新記録、全期間）
    loss_avg = data.get("weight_loss_avg_kg")
    loss_users = data.get("weight_loss_users", 0)
    if loss_avg is None:
        loss_avg_str = "—"
    elif loss_avg >= 0:
        loss_avg_str = f"{loss_avg:.1f} kg 減"
    else:
        loss_avg_str = f"{abs(loss_avg):.1f} kg 増"

    # 全体平均Bカウント（直近30日・1人1日あたり）
    b_overall = data.get("b_overall_avg")
    b_overall_str = f"{b_overall:.1f}" if b_overall is not None else "—"

    # 栄養素の平均（直近30日・1人1日あたり）
    nut = data.get("nutrition") or {}
    def _g(v):
        return f"{v:.0f}g" if v is not None else "—"
    protein_avg_str = _g(nut.get("protein_avg"))
    veg_avg_str     = _g(nut.get("veg_avg"))
    if nut.get("fruit_avg") is not None:
        fruit_avg_str, fruit_sub = _g(nut.get("fruit_avg")), f"計測{nut.get('fruit_days', 0)}人日"
    else:
        fruit_avg_str, fruit_sub = "収集中", "解析データに果物計測を追加済み"

    # 目的別比較テーブル
    gc = data.get("goal_compare") or {}
    GOAL_LABELS = [("cut", "🔥 減量希望"), ("maintain", "⚖️ 体重維持")]
    def _fmt(v, unit="", nd=1):
        return f"{v:.{nd}f}{unit}" if v is not None else "—"
    goal_rows = ""
    for key, label in GOAL_LABELS:
        g = gc.get(key) or {}
        goal_rows += f"""
        <tr style="border-bottom:1px solid #F3F4F6">
          <td style="padding:8px;font-weight:700;color:#374151">{label}</td>
          <td style="padding:8px;text-align:center">{g.get('users', 0)}名</td>
          <td style="padding:8px;text-align:center;font-weight:800;color:#FF6B35">{_fmt(g.get('avg_b'), ' B')}</td>
          <td style="padding:8px;text-align:center;font-weight:800;color:#374151">{_fmt(g.get('avg_protein'), 'g', 0)}</td>
          <td style="padding:8px;text-align:center;font-weight:800;color:#10B981">{_fmt(g.get('avg_veg'), 'g', 0)}</td>
        </tr>"""

    # Claude API クレジット状況（取得できなくてもレポートは出す）
    credit_section = _credit_section(credit or {"status": "error", "message":
                                                "クレジット情報を取得できませんでした。"},
                                     data.get("est_cost_jpy") or 0)

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
    <h1>🏋️ ABダイエット デイリーレポート</h1>
    <p>対象日: {rdate} &nbsp;|&nbsp; 配信: {now_str}</p>
  </div>
  <div class="body">

    {_coach_section(coach_advice)}

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

      <!-- 食事記録 -->
      <div style="margin-top:16px">
        <div style="font-size:12px;font-weight:700;color:#6B7280;margin-bottom:6px">
          食事記録（📸=写真 / 📝=テキスト）
        </div>
        <div class="kpi-row">
          <div class="kpi">
            <div class="kpi-lbl">記録件数</div>
            <div class="kpi-val" style="font-size:22px">{meal['count']}</div>
            <div class="kpi-sub">📸{meal['photo']} / 📝{meal['text']}</div>
          </div>
          <div class="kpi">
            <div class="kpi-lbl">記録した人数</div>
            <div class="kpi-val" style="font-size:22px">{meal['users']}</div>
            <div class="kpi-sub">ユーザー</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ① 日別推移グラフ -->
    <div class="section">
      <h2>📈 日別 利用推移（直近30日）</h2>
      <img class="chart" src="cid:chart_usage" alt="Usage Trend">
    </div>

    <!-- ② 体重・Bカウント・運動 推移 -->
    <div class="section">
      <h2>⚖️ 2. 体重 &amp; Bカウント &amp; 運動 推移</h2>
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-lbl">全体平均Bカウント</div>
          <div class="kpi-val" style="font-size:22px">{b_overall_str}</div>
          <div class="kpi-sub">回/日（直近30日・1人あたり）</div>
        </div>
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
        <div class="kpi">
          <div class="kpi-lbl">減量希望者 平均減量実績</div>
          <div class="kpi-val" style="font-size:22px;color:#F59E0B">{loss_avg_str}</div>
          <div class="kpi-sub">初回記録比・{loss_users}名平均</div>
        </div>
      </div>
      <img class="chart" src="cid:chart_b_count" alt="B-Count Trend" style="margin-top:12px">
      <img class="chart" src="cid:chart_weight" alt="Weight Trend" style="margin-top:6px">
      <img class="chart" src="cid:chart_weight_loss" alt="Weight Loss Progress" style="margin-top:6px">
      <div style="font-size:12px;font-weight:700;color:#6B7280;margin:14px 0 4px">
        減量希望者の「平均Bカウント」と「平均減量」の相関（Bを抑えた時期に減量が進んでいるか）
      </div>
      <img class="chart" src="cid:chart_cut_corr" alt="Cut Users B-Count vs Weight Loss">
      <img class="chart" src="cid:chart_exercise" alt="Exercise B-Count Trend" style="margin-top:6px">
    </div>

    <!-- ③ 栄養素の平均摂取量 -->
    <div class="section">
      <h2>🥗 3. 栄養素の平均摂取量（直近30日・1人1日あたり）</h2>
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-lbl">🥩 タンパク質</div>
          <div class="kpi-val" style="font-size:22px">{protein_avg_str}</div>
          <div class="kpi-sub">記録{nut.get('users', 0)}名平均</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">🥗 野菜</div>
          <div class="kpi-val" style="font-size:22px;color:#10B981">{veg_avg_str}</div>
          <div class="kpi-sub">目標 350g/日</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">🍎 果物</div>
          <div class="kpi-val" style="font-size:22px;color:#F59E0B">{fruit_avg_str}</div>
          <div class="kpi-sub">{fruit_sub}</div>
        </div>
      </div>
      <img class="chart" src="cid:chart_nutrition" alt="Nutrition Trend" style="margin-top:12px">
    </div>

    <!-- ④ 目的別比較 -->
    <div class="section">
      <h2>🎯 4. 目的別比較（減量希望 vs 体重維持）</h2>
      <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px">
        <tr style="border-bottom:2px solid #E5E7EB;color:#6B7280">
          <th style="padding:8px;text-align:left">目標</th>
          <th style="padding:8px">人数</th>
          <th style="padding:8px">平均Bカウント/日</th>
          <th style="padding:8px">平均タンパク質/日</th>
          <th style="padding:8px">平均野菜/日</th>
        </tr>{goal_rows}
      </table>
      <img class="chart" src="cid:chart_goal_compare" alt="Goal Comparison">
      <div style="font-size:11px;color:#9CA3AF;margin-top:4px">
        ※ 目標（減量/維持）は会員がアプリを利用した際に自動記録されます。未記録の会員は集計対象外です。
      </div>
    </div>

    <!-- ⑤ システム・運用状況 -->
    <div class="section">
      <h2>⚙️ 5. システム・運用状況</h2>

      <!-- 時間帯別分布 -->
      <div style="margin-bottom:14px">
        <div style="font-size:12px;font-weight:700;color:#6B7280;margin-bottom:6px">
          解析ピーク時間帯: <strong style="color:#FF6B35">{peak_h}:00〜{peak_h+1}:00</strong>（{peak_v}回）
        </div>
        <img class="chart" src="cid:chart_hourly" alt="Hourly Distribution">
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
          <td style="padding:8px;color:#9CA3AF">下の「6. Claude API クレジット状況」を参照</td>
        </tr>
      </table>
    </div>

    <!-- ⑥ Claude API クレジット状況 -->
    {credit_section}

  </div>
  <div class="footer">
    <p>ABダイエット 自動レポート | 毎朝 8:00 JST 配信</p>
    <p>Generated: {now_str}</p>
  </div>
</div>
</body>
</html>"""


# ── メール送信 ──────────────────────────────────────────────────
def send_email(subject: str, html_body: str, charts: dict):
    """グラフ画像はbase64埋め込みではなく cid 参照の inline 添付にする。
    base64直埋めだとHTML本文が数百KBに膨れ、Gmailの本文102KB自動クリッピングで
    画像が壊れ、切り詰められたbase64が文字化けのように本文へ露出してしまうため。"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"]    = GMAIL_USER
    msg["To"]      = REPORT_TO

    plain = f"ABダイエット デイリーレポート\nHTMLメールをご覧ください。\n生成時刻: {datetime.datetime.now(JST)}"
    msg.attach(MIMEText(plain, "plain", "utf-8"))

    related = MIMEMultipart("related")
    related.attach(MIMEText(html_body, "html", "utf-8"))
    for cid, png_bytes in charts.items():
        img = MIMEImage(png_bytes, name=f"{cid}.png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        related.attach(img)
    msg.attach(related)

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
        "chart_usage":    chart_usage(data),
        "chart_hourly":   chart_hourly(data),
        "chart_b_count":  chart_b_count(data),
        "chart_weight":   chart_weight(data),
        "chart_weight_loss": chart_weight_loss(data),
        "chart_cut_corr":  chart_cut_corr(data),
        "chart_exercise": chart_exercise(data),
        "chart_nutrition": chart_nutrition(data),
        "chart_goal_compare": chart_goal_compare(data),
    }

    print("Fetching Claude API credit / cost...")
    try:
        credit = build_credit_info()
    except Exception as e:   # noqa: BLE001 — クレジット取得の失敗でレポートを落とさない
        print(f"  credit info failed: {e}")
        credit = {"status": "error", "message": f"クレジット情報の取得に失敗しました（{e}）。"}
    print(f"  credit status={credit.get('status')} remaining={credit.get('remaining_usd')} "
          f"month={credit.get('spend_month_usd')}")
    if has_credit_chart(credit):
        charts["chart_credit"] = chart_credit(credit)

    print("Fetching AI coach advice...")
    coach_advice = fetch_coach_advice()
    print(f"  coach advice: {'OK (' + str(len(coach_advice)) + ' chars)' if coach_advice else 'skipped'}")

    print("Building HTML email...")
    rdate   = data["report_date"]
    subject = f"[ABダイエット] デイリーレポート {rdate}"
    html    = build_html(data, coach_advice, credit)

    if GMAIL_USER and GMAIL_PASS:
        print("Sending email...")
        send_email(subject, html, charts)
    else:
        out_path = f"report_{rdate}.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Email config missing — saved to {out_path}")


if __name__ == "__main__":
    main()
