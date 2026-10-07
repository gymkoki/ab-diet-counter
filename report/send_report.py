#!/usr/bin/env python3
"""
ABダイエット デイリーレポート送信スクリプト
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
try:
    from PIL import Image       # 会員の食事写真をメール用に縮小するのに使う
except ImportError:
    Image = None

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

# デイリーレポートの宛先（オーナー指示 2026-08）：
#   rits.1159@gmail.com へ送る。reallgym.tokyo 宛には送らない。
# GitHub Secrets の REPORT_TO に古いアドレス（reallgym.tokyo）が残っていても
# ここで弾いて既定の宛先に振り替える。
REPORT_TO_DEFAULT = "rits.1159@gmail.com"
REPORT_TO_BLOCKED = ("reallgym.tokyo@gmail.com",)


def _resolve_report_to(value: str) -> str:
    to = _clean_secret(value or "")
    if not to or to.lower() in REPORT_TO_BLOCKED:
        return REPORT_TO_DEFAULT
    return to


# ${{ secrets.REPORT_TO }} が未設定でも workflow 側で空文字の環境変数として渡されるため、
# os.environ.get の default 引数だけでは効かない。空文字化・空白混入の両方をここで吸収する。
REPORT_TO      = _resolve_report_to(os.environ.get("REPORT_TO", ""))

JST = datetime.timezone(datetime.timedelta(hours=9))

# この日数以上まったく記録がない会員は、写真のピックアップの対象外（app.py の REPORT_ABSENT_DAYS と同じ値）
REPORT_ABSENT_DAYS = 14

# カラーパレット（アプリのブランドカラーに準拠）
C_PRIMARY  = "#FF6B35"
C_INDIGO   = "#6366F1"
C_GREEN    = "#10B981"
C_AMBER    = "#F59E0B"
C_GRAY     = "#9CA3AF"
C_COST     = "#DC2626"   # API費用（円）の線


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


def _daily_cost_yen(data: dict, credit=None):
    """日別のAPI費用（円）と、その出どころ（"実額" / "推定"）を返す。

    クレジット情報に日別の金額があればそれを使う（Admin APIキーがあれば Cost API の実額、
    無ければアプリの解析回数からの推定）。取れなければ「解析回数 × 1回あたりの概算」で出す。
    ※Cost API の日付は UTC 区切りのため、JST の日付と最大9時間ずれる。"""
    dates = data["dates"]
    if credit and credit.get("status") in ("ok", "estimated") and credit.get("daily"):
        rate = credit.get("usd_jpy") or 155.0
        daily = credit["daily"]
        yen = [round(float(daily.get(d) or 0) * rate) for d in dates]
        return yen, ("推定" if credit.get("estimated") else "実額")
    per = data.get("cost_per_analysis") or 4
    return [round(a * per) for a in data["daily_analyses_trend"]], "推定"


def chart_usage(data: dict, credit=None) -> bytes:
    """日別の利用状況とAPI費用を1枚にまとめたグラフ（直近30日）。

    オーナー指示 2026-09-30：以前は「日別 利用推移」と「API 日別コスト」の似たグラフが
    2枚出ていたため1枚に統一した。必ず次の5項目を載せる：
      デイリー利用者数／食事解析の回数／APIの費用／食事記録の件数／記録した人数
    単位が3種類（回・件／人／円）あるので、棒＝回・件（左軸）、線＝人（右軸）、
    赤い線＝円（いちばん右の軸）に分けて描く。"""
    dates     = data["dates"]
    n         = len(dates)
    analyses  = data["daily_analyses_trend"]
    users     = data["daily_users_trend"]
    records   = data.get("daily_records_trend") or [0] * n
    recorders = data.get("daily_recorders_trend") or [0] * n
    cost_yen, cost_kind = _daily_cost_yen(data, credit)

    x = np.arange(n)
    tick_idx = [i for i in x if i % 5 == 0]
    tick_lbl = [dates[i][5:] for i in tick_idx]

    fig, ax1 = plt.subplots(figsize=(10, 4.8))
    ax2 = ax1.twinx()                 # 人数
    ax3 = ax1.twinx()                 # 円
    ax3.spines["right"].set_position(("axes", 1.10))

    w = 0.4
    b1 = ax1.bar(x - w / 2, analyses, width=w, color=C_INDIGO + "CC", zorder=2)
    b2 = ax1.bar(x + w / 2, records,  width=w, color=C_GREEN + "B3",  zorder=2)
    l1, = ax2.plot(x, users, color=C_PRIMARY, marker="o", markersize=4, linewidth=2.2, zorder=3)
    l2, = ax2.plot(x, recorders, color=C_AMBER, marker="s", markersize=3.5, linewidth=2.0,
                   linestyle="--", zorder=3)
    l3, = ax3.plot(x, cost_yen, color=C_COST, marker="D", markersize=3, linewidth=1.8, zorder=4)

    ax1.set_xticks(tick_idx)
    ax1.set_xticklabels(tick_lbl, fontsize=12)
    ax1.set_ylabel("回・件（棒）", fontsize=12, color=C_INDIGO)
    ax2.set_ylabel("人（上の線）", fontsize=12, color=C_PRIMARY)
    ax3.set_ylabel("円（赤い線）", fontsize=12, color=C_COST)
    for ax, col in ((ax1, C_INDIGO), (ax2, C_PRIMARY), (ax3, C_COST)):
        ax.tick_params(axis="y", colors=col, labelsize=11)

    # 5本が重なって読めなくならないよう、上下の帯に分けて描く：
    #   下の帯（〜55%）＝棒（回・件）と赤い線（円）。推定のときは費用＝回数×単価なので、
    #                    赤い線が解析回数の棒の頭をなぞる形になり、関係が一目で分かる。
    #   上の帯（60〜95%）＝人数の2本の線。
    def _band(ax, vals, lo, hi, zero_based):
        vmax = max(vals or [0]) or 1
        if zero_based:
            top = vmax / hi
            ax.set_ylim(0, top)
            ticks = [t for t in ax.get_yticks() if 0 <= t <= vmax * 1.05]
        else:
            vmin = min(vals or [0])
            span = max(vmax - vmin, 1) / (hi - lo)
            bottom = vmin - lo * span
            ax.set_ylim(bottom, bottom + span)
            step = 5 if vmax - vmin <= 30 else 10
            first = int(np.ceil(vmin / step) * step)
            ticks = list(range(first, int(vmax) + 1, step)) or [round(vmin)]
        ax.set_yticks(ticks)

    _band(ax1, list(analyses) + list(records), 0, 0.55, zero_based=True)
    _band(ax3, cost_yen, 0, 0.55, zero_based=True)
    _band(ax2, list(users) + list(recorders), 0.60, 0.95, zero_based=False)

    total = sum(cost_yen)
    how = "1回¥{:g}で計算".format(data.get("cost_per_analysis") or 4) if cost_kind == "推定" \
        else "Anthropic Cost API"
    ax1.set_title(f"日別 利用状況とAPI費用（直近30日） — API費用 30日合計 ¥{total:,}（{cost_kind}・{how}）",
                  fontsize=13.5, fontweight="bold", pad=8)

    ax1.legend([b1, b2, l1, l2, l3],
               ["食事解析の回数", "食事記録の件数", "デイリー利用者数", "記録した人数", "APIの費用（円）"],
               loc="upper center", bbox_to_anchor=(0.5, -0.10), ncol=5, fontsize=10.5,
               frameon=False, handlelength=1.6, columnspacing=1.2)

    for ax in (ax1, ax2, ax3):
        ax.spines["top"].set_visible(False)
    ax1.grid(axis="y", alpha=0.25, zorder=0)
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


# ※運動でのBカウント消費を集団平均で見せる棒グラフは、オーナー指示（2026-08）により削除した。
#   会員ごとの推移は管理画面の個人ページで見られるため、レポートには載せない。


def _axes_note(ax, msg: str):
    """データがまだ無いとき、グラフの代わりに案内文を表示する。"""
    ax.text(0.5, 0.5, msg, ha="center", va="center", fontsize=13,
            color=C_GRAY, transform=ax.transAxes, linespacing=1.8)
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)


def chart_cut_corr(data: dict) -> bytes:
    """減量希望者：横軸＝平均Bカウント、縦軸＝体重の変化 の散布図（1人1点）。

    以前は「日ごとの集団平均B」と「集団平均の減量幅」を2本の折れ線で並べていたが、
    横軸が日付だったため“BとBの結果の関係”が読み取れなかった（オーナー指摘 2026-08）。
    会員1人を1点にすると「Bを抑えている人ほど減っているか」が一目で分かる。
    縦軸はマイナスが減量（下にあるほど痩せている）。"""
    cc      = data.get("cut_corr") or {}
    members = cc.get("members") or []
    users   = cc.get("users", 0)
    min_days = cc.get("min_days", 3)

    fig, ax = plt.subplots(figsize=(10, 4.6))
    title = "減量希望者：平均Bカウント × 体重の変化"

    if not members:
        ax.set_title(title, fontsize=15, fontweight="bold", pad=8)
        _axes_note(ax, f"データを収集中です。\nBカウントの記録が{min_days}日以上あり、体重を2回以上記録した\n減量希望の会員が対象です。")
        fig.tight_layout()
        return fig_to_png(fig)

    xs = [m["avg_b"] for m in members]
    ys = [m["change_kg"] for m in members]

    # 減った人と増えた人で色を分ける（0kgちょうどは「維持」として増加側の色にしない）
    lost = [(x, y) for x, y in zip(xs, ys) if y < 0]
    gain = [(x, y) for x, y in zip(xs, ys) if y >= 0]
    if lost:
        ax.scatter(*zip(*lost), s=90, color=C_GREEN, alpha=0.75, zorder=3,
                   edgecolors="white", linewidths=1.2, label=f"減量できている（{len(lost)}名）")
    if gain:
        ax.scatter(*zip(*gain), s=90, color=C_PRIMARY, alpha=0.75, zorder=3,
                   edgecolors="white", linewidths=1.2, label=f"減っていない（{len(gain)}名）")

    # 傾向線と相関係数（3人以上いて、Bカウントにばらつきがあるときだけ）
    note = ""
    if len(members) >= 3 and len(set(xs)) > 1:
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        syy = sum((y - my) ** 2 for y in ys)
        slope = sxy / sxx
        intercept = my - slope * mx
        x0, x1 = min(xs), max(xs)
        ax.plot([x0, x1], [slope * x0 + intercept, slope * x1 + intercept],
                color=C_GRAY, linewidth=2, linestyle="--", zorder=2,
                label="傾向線")
        if syy > 0:
            r = sxy / ((sxx ** 0.5) * (syy ** 0.5))
            # 正の相関＝Bが多いほど体重が増える方向。ABダイエットの狙いどおりの並び。
            if r >= 0.3:
                verdict = "Bが多い人ほど減っていない傾向"
            elif r <= -0.3:
                verdict = "Bが多い人ほど減っている（想定と逆）"
            else:
                verdict = "はっきりした関係は見られない"
            note = f"相関 r = {r:+.2f}（{verdict}）"

    ax.axhline(0, color=C_GRAY, linewidth=1.2, zorder=1)

    ax.set_xlabel("平均Bカウント / 日（直近30日）", fontsize=13)
    ax.set_ylabel("体重の変化 (kg)　※マイナス＝減量", fontsize=13)
    ax.set_xlim(left=0)
    # タイトルは短く保ち、内訳と相関はその下の1行にまとめる（重ならないよう pad を確保）
    ax.set_title(title + "（1人1点・直近30日）", fontsize=15, fontweight="bold", pad=30)
    detail = f"{len(members)}名（減量希望{users}名中・Bの記録が{min_days}日以上ある人）"
    if note:
        detail += f"　｜　{note}"
    ax.text(0.5, 1.015, detail, transform=ax.transAxes, ha="center", va="bottom",
            fontsize=12, color=C_GRAY)
    ax.tick_params(axis="both", labelsize=13)
    ax.legend(loc="best", fontsize=12)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_png(fig)


# ※「体重が2kg以上ふえた人／へった人の朝昼晩の記録状況」と「記録が足りない会員」の表
#   （chart_gained_slots / chart_lost_slots / chart_thin_slots）は、
#   オーナー指示 2026-10-01「いらない。今後は記載するな」で削除した。復活させないこと。
#   記録が足りない会員を Bカウントの分析（相関・平均B）から外す処理はサーバー側に残っている。


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


# 減量希望者の成功群／失敗群で比べる項目：(キー, 表示名, 単位, 小数桁)
CUT_COMPARE_METRICS = (
    ("kcal",      "摂取カロリー", "kcal", 0),
    ("protein_g", "タンパク質",   "g",    0),
    ("veg_g",     "野菜",         "g",    0),
    ("b_count",   "Bカウント",    "回",   1),   # オーナー要望 2026-10-07「Bの平均数も知りたい」
)
C_SUCCESS = C_GREEN   # 減量成功群
C_FAIL    = C_GRAY    # 減量失敗群（減っていない）


def _cut_metric(csc: dict, key: str) -> dict:
    for r in (csc or {}).get("results") or []:
        if r.get("key") == key:
            return r
    return {}


def chart_goal_compare(data: dict) -> bytes:
    """④ 減量希望者：減量成功群 vs 失敗群の摂取カロリー・タンパク質・野菜・Bカウント（1人1日あたり）。
    （オーナー指示 2026-10-01：「減量希望と体重維持」の比較から置き換え）"""
    csc = data.get("cut_success_compare") or {}
    cov = csc.get("coverage") or {}
    n_ok, n_ng = cov.get("loss_n", 0), cov.get("gain_n", 0)

    fig, axes = plt.subplots(1, len(CUT_COMPARE_METRICS), figsize=(3.2 * len(CUT_COMPARE_METRICS), 3.9))
    title = "減量希望者：減量成功群 vs 失敗群（1人1日あたりの平均）"

    if not n_ok and not n_ng:
        fig.suptitle(title, fontsize=15, fontweight="bold")
        notes = ("比較できる会員が\nまだいません。",
                 "体重を2回以上（7日以上の幅で）\n記録した減量希望者が対象です。",
                 "3食以上記録した日だけを\n集計します。")
        for i, ax in enumerate(axes):
            if i < len(notes):
                _axes_note(ax, notes[i])
            else:
                ax.axis("off")
        fig.tight_layout()
        return fig_to_png(fig)

    groups = [("loss", f"成功群\n({n_ok}名)", C_SUCCESS), ("gain", f"失敗群\n({n_ng}名)", C_FAIL)]
    for ax, (key, label, unit, nd) in zip(axes, CUT_COMPARE_METRICS):
        r = _cut_metric(csc, key)
        xs = np.arange(len(groups))
        vals = [((r.get(g) or {}).get("mean")) for g, _, _ in groups]
        ax.bar(xs, [v or 0 for v in vals], width=0.55, color=[c for _, _, c in groups], zorder=2)
        top = max([v for v in vals if v is not None] or [1])
        for x, v in zip(xs, vals):
            txt = f"{v:,.{nd}f}" if v is not None else "—"
            ax.text(x, (v or 0) + top * 0.02, txt, ha="center", va="bottom",
                    fontsize=13, fontweight="bold")
        ax.set_ylim(0, top * 1.22)
        ax.set_xticks(xs)
        ax.set_xticklabels([lbl for _, lbl, _ in groups], fontsize=11)
        sub = label
        if r.get("diff") is not None:
            sign = "+" if r["diff"] > 0 else "−"
            sub += f"\n成功群 {sign}{abs(r['diff']):,.{nd}f}{unit}" + (" ＊" if r.get("significant") else "")
        ax.set_title(sub, fontsize=13, fontweight="bold")
        ax.set_ylabel(f"{unit} / 人・日", fontsize=11)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(axis="y", alpha=0.25, zorder=0)

    fig.suptitle(title, fontsize=15, fontweight="bold")
    fig.tight_layout()
    return fig_to_png(fig)


# ── メール HTML 本文 ────────────────────────────────────────────
# 画像は cid:chart_usage / cid:chart_weight_loss などで参照する
# （send_email() が main() で生成した charts dict のキーと同名の Content-ID を付けて添付する）。
# ※API費用の日別グラフ（chart_credit）は、オーナー指示 2026-09-30 で chart_usage に統合した。
#   似たグラフが2枚並んでいたため。費用の推移は chart_usage の赤い線で見る。


def _credit_section(credit: dict, est_cost_jpy: int) -> str:
    """レポート最上部のAPIコストのカード（昨日／今月／1日あたり平均）。

    【オーナー指示 2026-10-03】「💳 Claude API クレジット状況」の見出しと、
    「クレジット残高」「残り日数の目安（次の自動チャージ）」の2枚はレポートに載せない
    （基準残高が未登録だと「未設定」「—」が並ぶだけで役に立たなかったため）。
    残すのはコストの数字だけ。黄色の補足メッセージ・※注記も 2026-10-01 に削除済み。戻さないこと。
    """
    rate = credit.get("usd_jpy") or 155.0

    def _yen(usd):
        return f"¥{round(usd * rate):,}" if usd is not None else "—"

    def _usd(usd):
        return f"${usd:,.2f}" if usd is not None else "—"

    status = credit.get("status")
    kind = "推定" if credit.get("estimated") else "実額"

    if status not in ("ok", "estimated"):
        # 実額も推定も出せないときは、解析回数からの概算だけを出す
        cards = f"""
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-lbl">昨日の推定コスト</div>
          <div class="kpi-val" style="font-size:22px">¥{est_cost_jpy:,}</div>
          <div class="kpi-sub">解析回数からの概算</div>
        </div>
      </div>"""
    else:
        # 月間支出上限：これに達すると残高があってもAPIが止まるので、超えそうなら赤で警告
        limit = credit.get("spend_limit_usd")
        month_color = ""
        if limit:
            pct = credit.get("spend_month_pct")
            month_sub = f"{_usd(credit.get('spend_month_usd'))}／上限 {_usd(limit)}（{pct:.0f}%）"
            if credit.get("limit_risk"):
                month_color = ";color:#EF4444"
                month_sub += f"｜月末見込み {_usd(credit.get('projected_month_usd'))}⚠"
        else:
            month_sub = _usd(credit.get("spend_month_usd"))

        cards = f"""
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-lbl">昨日のコスト（{kind}）</div>
          <div class="kpi-val" style="font-size:20px">{_yen(credit.get('spend_yesterday_usd'))}</div>
          <div class="kpi-sub">{_usd(credit.get('spend_yesterday_usd'))}</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">今月のコスト（{kind}）</div>
          <div class="kpi-val" style="font-size:20px{month_color}">{_yen(credit.get('spend_month_usd'))}</div>
          <div class="kpi-sub">{month_sub}</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">1日あたり平均</div>
          <div class="kpi-val" style="font-size:20px">{_yen(credit.get('spend_7d_avg_usd'))}</div>
          <div class="kpi-sub">直近7日平均</div>
        </div>
      </div>"""

    # 日別の費用グラフはここには出さない（「📈 日別 利用状況とAPI費用」の1枚に統合済み）
    return f"""
    <div class="section">{cards}
    </div>"""


def fetch_credit_estimate():
    """残高推定の材料（アプリ自身の日別解析回数＋登録済みの基準残高）を取得する。

    Admin APIキーが無くても「だいたいの残高」を出すために使う。
    取れなくてもレポートは送る（実額が取れていればそちらが優先される）。
    """
    try:
        r = requests.get(
            f"{APP_URL}/api/admin/credit-estimate",
            auth=(ADMIN_USER, ADMIN_PASS),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"credit-estimate skipped: {e}")
        return None


def fetch_report_photos():
    """減量が順調な会員／進んでいない会員の、実際の食事写真を取得する。
    失敗してもレポート本体は送る（写真セクションだけ省略）。"""
    for attempt in range(2):
        try:
            r = requests.get(
                f"{APP_URL}/api/admin/report-photos",
                auth=(ADMIN_USER, ADMIN_PASS),
                timeout=240,  # 全員ぶんの写真を縮小して返すため時間がかかる
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"report-photos retry {attempt + 1}: {e}")
            time.sleep(5)
    return None


def _decode_photo(data_uri: str, max_side: int = 320):
    """data:image のbase64をJPEGバイト列に戻し、メール用に縮小する。
    サーバー側でサムネ化済みだが、古い形式のデータが混ざっても大きくなりすぎない
    よう、ここでも長辺320pxに収める（並べて見比べるにはこの大きさで十分）。"""
    try:
        b64 = data_uri.split(",", 1)[1]
    except IndexError:
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    if Image is None:                       # Pillowが無い環境ではそのまま使う
        return raw
    try:
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=72, optimize=True)
        return buf.getvalue()
    except Exception:
        return raw


# セクション全体で載せる写真の上限。枚数は制限しない方針だが、Gmailは本文が
# 約102KBを超えるとメールを途中で切ってしまう（＝以降が読めなくなる）。
# 1枚あたり約250バイトなので、この枚数なら本文は75KB程度で収まる。
MAX_PHOTOS_TOTAL = 300


def chart_progress(progress: dict, good: bool) -> bytes:
    """会員1人の「始めたとき → いま」の推移グラフ（オーナー指示 2026-09-30）。

    ・体重の折れ線（全期間）。開始時の体重に点線を引き、上下どちらにいるか一目で分かるようにする
    ・その日のBカウントを薄い棒で下に重ねる（体重が動いた時期に何を食べていたかを見るため）
    """
    import matplotlib.dates as mdates

    color = C_GREEN if good else "#EF4444"
    ws = [(datetime.date.fromisoformat(d), w) for d, w in (progress.get("weights") or [])]
    bs = [(datetime.date.fromisoformat(d), b) for d, b in (progress.get("b_daily") or [])]

    fig, ax = plt.subplots(figsize=(7.2, 2.3))
    ax2 = ax.twinx()

    # Bカウント（右軸・薄い棒）。棒は下半分に収め、体重の線と重ならないようにする
    if bs:
        bx, by = zip(*bs)
        ax2.bar(bx, by, width=0.8, color="#CBD5E1", alpha=0.55, zorder=1)
        ax2.set_ylim(0, max(max(by), 1) * 2.4)
        ax2.set_ylabel("B/日", fontsize=10, color="#94A3B8")
        ax2.tick_params(axis="y", colors="#94A3B8", labelsize=9)
    else:
        ax2.set_yticks([])
    ax2.spines["top"].set_visible(False)
    ax2.spines["right"].set_color("#E2E8F0")

    # 体重（左軸・折れ線）
    ax.set_zorder(ax2.get_zorder() + 1)
    ax.patch.set_visible(False)
    wx, wy = zip(*ws)
    ax.plot(wx, wy, color=color, linewidth=2.2, marker="o", markersize=3.5, zorder=3)
    start_w = progress["start_weight"]
    ax.axhline(start_w, color="#94A3B8", linewidth=1, linestyle="--", zorder=2)
    ax.annotate(f"開始 {start_w:.1f}kg", (wx[0], wy[0]), textcoords="offset points",
                xytext=(4, 8), fontsize=9, color="#475569")
    if len(ws) > 1:
        # 線が上から下りてくる（減っている）ときは下側に、上がってくるときは上側に置き、
        # ラベルが線や点に重ならないようにする
        recent = wy[-6:-1] or wy[:1]
        going_down = wy[-1] <= sum(recent) / len(recent)
        ax.annotate(f"現在 {wy[-1]:.1f}kg", (wx[-1], wy[-1]), textcoords="offset points",
                    xytext=(-6, -15 if going_down else 9), fontsize=9, color=color,
                    fontweight="bold", ha="right",
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85))
    pad = max(0.6, (max(wy) - min(wy)) * 0.35)
    ax.set_ylim(min(wy) - pad, max(wy) + pad)
    ax.set_ylabel("体重 kg", fontsize=10)
    ax.tick_params(axis="y", labelsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-m/%-d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=7))
    ax.tick_params(axis="x", labelsize=9)
    ax.spines["top"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.tight_layout()
    return fig_to_png(fig)


def _progress_block(progress, cid: str, charts: dict, good: bool) -> str:
    """カード内の「開始時 → 現在」の要約とグラフ。体重の記録が無ければ空。"""
    if not progress:
        return ""
    try:
        charts[cid] = chart_progress(progress, good)
        img = (f'<img src="cid:{cid}" alt="推移" '
               f'style="width:100%;max-width:640px;display:block;margin:6px 0 10px;border-radius:6px">')
    except Exception as e:          # グラフが描けなくても要約とレポート本体は出す
        print(f"progress chart skipped: {e}")
        img = ""

    def md(d):
        try:
            x = datetime.date.fromisoformat(d)
            return f"{x.month}/{x.day}"
        except (TypeError, ValueError):
            return "—"

    ch = progress.get("total_change_kg")
    pct = progress.get("total_change_pct")
    days = progress.get("days_since_start") or 0
    if progress.get("weight_records", 0) < 2:
        change_html = '<span style="color:#9CA3AF">体重の記録が開始時の1回だけです</span>'
    else:
        c = "#059669" if (ch or 0) < 0 else ("#DC2626" if (ch or 0) > 0 else "#6B7280")
        pct_s = f"（{pct:+.1f}%）" if isinstance(pct, (int, float)) else ""
        change_html = (f'<b style="color:{c}">{ch:+.1f}kg{pct_s}</b>'
                       f'<span style="color:#9CA3AF">／{days}日間</span>')

    b0, b1 = progress.get("b_first_week_avg"), progress.get("b_last_week_avg")
    if isinstance(b0, (int, float)) and isinstance(b1, (int, float)):
        bc = "#059669" if b1 < b0 else ("#DC2626" if b1 > b0 else "#6B7280")
        b_html = (f'食べ方（1日の平均B）：開始直後 {b0:g} → 直近 '
                  f'<b style="color:{bc}">{b1:g}</b>')
    elif isinstance(b1, (int, float)):
        b_html = f'食べ方（1日の平均B）：直近 {b1:g}（開始直後の記録が足りず比較なし）'
    else:
        b_html = ""

    return f"""
      <div style="font-size:12px;color:#374151;line-height:1.7;margin-bottom:2px">
        📈 <b>開始時 {progress['start_weight']:.1f}kg</b>（{md(progress.get('start_date'))}）
        → <b>現在 {progress['latest_weight']:.1f}kg</b>（{md(progress.get('latest_date'))}）　{change_html}
        {f'<br>{b_html}' if b_html else ''}
      </div>
      {img}"""


def _photo_card(member: dict, cid_prefix: str, charts: dict, good: bool, budget: int = MAX_PHOTOS_TOTAL) -> str:
    """会員1人ぶんの写真カード（名前・体重変化・その人の写真すべて）を組み立てる。
    budget は「セクション全体であと何枚載せられるか」。"""
    color = "#10B981" if good else "#EF4444"
    ch = member.get("change_30d_kg")
    if isinstance(ch, (int, float)):
        change_str = f"{ch:+.1f} kg" if ch else "±0.0 kg"
    else:
        change_str = "—"
    avg_b = member.get("avg_b_7d")
    target = member.get("b_target")
    b_str = f"平均 {avg_b:.1f} B/日" if isinstance(avg_b, (int, float)) else "Bカウント —"
    if isinstance(target, (int, float)):
        b_str += f"（目標 {target:.0f} 以内）"

    # 写真は1行3枚で折り返す（枚数に制限を設けず全部載せる／オーナー指示 2026-09-23）
    PER_ROW = 3
    cells = []
    for i, ph in enumerate(member.get("photos") or []):
        if len(cells) >= budget:
            break
        png = _decode_photo(ph.get("src") or "")
        if not png:
            continue
        cid = f"{cid_prefix}_{i}"
        charts[cid] = png
        b = ph.get("b_count")
        b_lbl = f"B {b:g}" if isinstance(b, (int, float)) else "B —"
        p = ph.get("protein_g")
        v = ph.get("veg_g")
        sub = " / ".join(x for x in [
            b_lbl,
            f"P {p:g}g" if isinstance(p, (int, float)) else "",
            f"野菜 {v:g}g" if isinstance(v, (int, float)) else "",
        ] if x)
        foods = (ph.get("foods") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        day_b = ph.get("day_b_count")
        day_lbl = f"　この日の合計 B{day_b:g}" if isinstance(day_b, (int, float)) else ""
        cells.append(
            f'<td class="pc"><img src="cid:{cid}" alt="meal" class="pi">'
            f'<div class="pt">{ph.get("date", "")}{day_lbl}<br>'
            f'<b class="{"pg" if good else "pb"}">{sub}</b><br>'
            f'<span class="pf">{foods}</span></div></td>'
        )
    if not cells:
        return ""

    # 開始時 → 現在 の推移（写真のあるカードにだけ付ける＝使わない画像を添付しない）
    progress_html = _progress_block(member.get("progress"), f"{cid_prefix}_progress", charts, good)

    rows = "".join(
        f"<tr>{''.join(cells[i:i + PER_ROW])}</tr>"
        for i in range(0, len(cells), PER_ROW)
    )
    name = str(member.get("name") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""
    <div style="margin-bottom:14px;padding:10px 12px;background:#fff;border:1px solid #E5E7EB;border-left:4px solid {color};border-radius:8px">
      <div style="font-size:13px;font-weight:800;color:#374151;margin-bottom:8px">
        {name}
        <span style="color:{color};margin-left:8px">30日 {change_str}</span>
        <span style="font-size:11px;color:#9CA3AF;font-weight:600;margin-left:8px">{b_str}</span>
        <span style="font-size:11px;color:#9CA3AF;font-weight:600;margin-left:8px">写真 {len(cells)}枚</span>
      </div>{progress_html}
      <table cellpadding="0" cellspacing="0" border="0">{rows}</table>
    </div>"""


def _photo_section(photos: dict, charts: dict) -> str:
    """「実際の食事写真」セクション。数字よりも先に、目で見て違いが分かるように最上部へ置く。
    写真が1枚も取れなかった日は、セクションごと省略する。"""
    if not photos:
        return ""
    good = photos.get("good") or []
    bad  = photos.get("bad") or []
    days = photos.get("photo_days", 7)

    # 枚数は制限しないが、セクション全体の上限だけは守る（Gmailの本文切れ防止）
    def _cards(members, prefix, is_good):
        out = ""
        for i, mem in enumerate(members):
            left = MAX_PHOTOS_TOTAL - len(charts)
            if left <= 0:
                break
            out += _photo_card(mem, f"{prefix}{i}", charts, is_good, left)
        return out

    good_html = _cards(good, "ph_good", True)
    bad_html  = _cards(bad, "ph_bad", False)
    if not good_html and not bad_html:
        return ""

    blocks = ""
    if good_html:
        blocks += f"""
      <div style="margin-bottom:6px">
        <div style="font-size:13px;font-weight:800;color:#059669;margin-bottom:8px">
          ✅ 減量が順調な会員の食事（直近{days}日）
        </div>
        {good_html}
      </div>"""
    else:
        blocks += """
      <div style="font-size:12px;color:#9CA3AF;margin-bottom:10px">
        減量が順調な会員の写真は、今回は見つかりませんでした。
      </div>"""
    if bad_html:
        blocks += f"""
      <div>
        <div style="font-size:13px;font-weight:800;color:#DC2626;margin:14px 0 8px">
          ⚠️ 減量が進んでいない会員の食事（直近{days}日）
        </div>
        {bad_html}
      </div>"""
    else:
        blocks += """
      <div style="font-size:12px;color:#9CA3AF;margin-top:10px">
        減量が進んでいない会員の写真は、今回は見つかりませんでした。
      </div>"""

    return f"""
    <div class="section" style="background:#F9FAFB;border:1px solid #E5E7EB;border-radius:12px;padding:16px 18px">
      <h2>🍽️ 実際の食事写真｜順調な人 vs 進んでいない人</h2>
      <div style="font-size:11px;color:#9CA3AF;line-height:1.6;margin-bottom:12px">
        直近30日の体重変化で分けた、減量希望の会員の実際の記録です。
        <b>この内容は会員の食事写真を含むため、オーナー限定</b>（このメールの宛先のみ）です。
        ※{REPORT_ABSENT_DAYS}日以上まったく記録がない会員は対象外です。
        ※毎日、しばらく載っていない人から順に入れ替えています。
      </div>
      {blocks}
    </div>
    """


# 「AI減量コーチ｜今日の提案」はオーナー指示（2026-09-30）でレポートから削除した。
# 中身が役に立たないうえ、載せるたびにAIを呼んで費用もかかっていたため。戻さないこと。
def _sweet_rule_section(sr) -> str:
    """🍬 お菓子・スイーツ・ジュースの特別ルールが、昨日何人に適用されたか（オーナー依頼 2026-10-07）。
    ルールは1人1日1回まで（その日の合計が120〜200kcalで+0.5、200kcal超で+1）なので、
    適用人数＝適用回数。直近7日の推移も小さな表で並べる。人数だけで名前は出さない。"""
    if not sr or not sr.get("day"):
        return ""
    day = sr["day"]
    half_k, full_k = sr.get("half_kcal", 120), sr.get("full_kcal", 200)
    if not day.get("measured"):
        body = """<div style="font-size:13px;color:#6B7280;line-height:1.7">
          まだ集計できるデータがありません（この集計は導入日以降の記録から数えます）。</div>"""
    else:
        rate = day.get("rate")
        rate_str = f"{rate}%" if rate is not None else "—"
        body = f"""
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-lbl">特別ルールが適用された人</div>
          <div class="kpi-val" style="font-size:24px">{day['applied']}人</div>
          <div class="kpi-sub">記録した{day['measured']}人中・{rate_str}</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">＋0.5（合計{half_k}〜{full_k}kcal）</div>
          <div class="kpi-val" style="font-size:22px;color:#F59E0B">{day['half']}人</div>
          <div class="kpi-sub">&nbsp;</div>
        </div>
        <div class="kpi">
          <div class="kpi-lbl">＋1（合計{full_k}kcal超）</div>
          <div class="kpi-val" style="font-size:22px;color:#EF4444">{day['full']}人</div>
          <div class="kpi-sub">&nbsp;</div>
        </div>
      </div>"""
    rows = ""
    for t in sr.get("trend") or []:
        md = t["date"][5:].replace("-", "/")
        if t.get("measured"):
            cells = (f"<td style='padding:5px 6px;text-align:center;font-weight:800'>{t['applied']}人</td>"
                     f"<td style='padding:5px 6px;text-align:center;color:#F59E0B'>{t['half']}</td>"
                     f"<td style='padding:5px 6px;text-align:center;color:#EF4444'>{t['full']}</td>"
                     f"<td style='padding:5px 6px;text-align:center;color:#6B7280'>{t['measured']}人</td>")
        else:
            # 記録はあるが導入前の日＝未計測／そもそも記録が無い日＝記録なし
            lbl = "未計測" if t.get("recorded") else "記録なし"
            cells = f"<td colspan='4' style='padding:5px 6px;text-align:center;color:#9CA3AF'>{lbl}</td>"
        rows += f"<tr style='border-bottom:1px solid #F3F4F6'><td style='padding:5px 6px'>{md}</td>{cells}</tr>"
    table = f"""
      <table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:10px">
        <tr style="border-bottom:2px solid #E5E7EB;color:#6B7280">
          <th style="padding:5px 6px;text-align:left">日付</th><th style="padding:5px 6px">適用</th>
          <th style="padding:5px 6px">＋0.5</th><th style="padding:5px 6px">＋1</th>
          <th style="padding:5px 6px">記録した人</th>
        </tr>{rows}
      </table>""" if rows else ""
    return f"""
    <div class="section">
      <h2>🍬 お菓子・スイーツ・ジュースの特別ルール（{sr['date']}）</h2>
      {body}{table}
      <div style="font-size:11px;color:#9CA3AF;margin-top:6px;line-height:1.6">
        ※ 1回ではB0だったお菓子・スイーツ・ジュースの1日の合計で判定。<b>1人1日1回まで</b>なので、適用人数＝適用回数です。
      </div>
    </div>"""


def build_html(data: dict, credit=None, photo_section="") -> str:
    rdate = data["report_date"]
    meal  = data["meal_summary"]
    now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

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

    # ④ 減量希望者：成功群 vs 失敗群のテーブル
    csc = data.get("cut_success_compare") or {}
    cov = csc.get("coverage") or {}
    def _fmt(v, unit="", nd=1):
        return f"{v:,.{nd}f}{unit}" if v is not None else "—"
    cut_rows = ""
    for key, label, unit, nd in CUT_COMPARE_METRICS:
        r = _cut_metric(csc, key)
        ok = (r.get("loss") or {}).get("mean")
        ng = (r.get("gain") or {}).get("mean")
        diff = r.get("diff")
        if diff is None:
            diff_str = "—"
        else:
            diff_str = ("+" if diff > 0 else "−") + _fmt(abs(diff), unit, nd)
            if r.get("significant"):
                diff_str += " ＊"
        cut_rows += f"""
        <tr style="border-bottom:1px solid #F3F4F6">
          <td style="padding:8px;font-weight:700;color:#374151">{label}</td>
          <td style="padding:8px;text-align:center;font-weight:800;color:#10B981">{_fmt(ok, unit, nd)}</td>
          <td style="padding:8px;text-align:center;font-weight:800;color:#6B7280">{_fmt(ng, unit, nd)}</td>
          <td style="padding:8px;text-align:center;font-weight:800;color:#374151">{diff_str}</td>
        </tr>"""
    # 記録のしかた（写真の枚数・間食など）の比較（オーナー指摘 2026-10-07：
    # 「失敗群の方がカロリーが少ないのはおかしい。写真をアップしていないのでは？」の検証）
    rec = csc.get("record") or {}
    rec_rows = ""
    for r in rec.get("results") or []:
        nd, unit = r.get("digits", 1), r.get("unit", "")
        ok = (r.get("loss") or {}).get("mean")
        ng = (r.get("gain") or {}).get("mean")
        diff = r.get("diff")
        if diff is None:
            diff_str = "—"
        else:
            diff_str = ("+" if diff > 0 else "−") + _fmt(abs(diff), unit, nd)
            if r.get("significant"):
                diff_str += " ＊"
        rec_rows += f"""
        <tr style="border-bottom:1px solid #F3F4F6">
          <td style="padding:8px;font-weight:700;color:#374151">{r.get('label', '')}
            <div style="font-size:11px;font-weight:400;color:#9CA3AF">{r.get('per') or ''}</div></td>
          <td style="padding:8px;text-align:center;font-weight:800;color:#10B981">{_fmt(ok, unit, nd)}</td>
          <td style="padding:8px;text-align:center;font-weight:800;color:#6B7280">{_fmt(ng, unit, nd)}</td>
          <td style="padding:8px;text-align:center;font-weight:800;color:#374151">{diff_str}</td>
        </tr>"""
    if rec_rows:
        rec_html = f"""
      <h3 style="font-size:14px;margin:18px 0 6px;color:#374151">📷 記録のしかたの違い
        <span style="font-weight:400;color:#6B7280">（成功群 {rec.get('loss_n', 0)}名／失敗群 {rec.get('gain_n', 0)}名）</span></h3>
      <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:6px">
        <tr style="border-bottom:2px solid #E5E7EB;color:#6B7280">
          <th style="padding:8px;text-align:left">項目</th>
          <th style="padding:8px">成功群</th>
          <th style="padding:8px">失敗群</th>
          <th style="padding:8px">差（成功−失敗）</th>
        </tr>{rec_rows}
      </table>
      <div style="font-size:11px;color:#9CA3AF">
        ※ 記録漏れそのものを見るため、こちらは3食未満の日も含めて数えています（直近60日）。
        「間食・追加の記録」は朝（4〜10時）・昼（10〜16時）・晩（16〜翌4時）の各時間帯で2件目以降の記録の数。
        写真の枚数は「写真で記録」した回数です。
      </div>"""
    else:
        rec_html = ""
    cut_n_ok, cut_n_ng = cov.get("loss_n", 0), cov.get("gain_n", 0)
    cut_note = ("※ 人数が少ないため参考値です。" if cov.get("small_sample") else "")

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
  /* 食事写真ギャラリー：枚数を制限せず全部載せるため、1枚あたりのHTMLを短くして
     Gmailの本文サイズ制限（約102KB・超えると途中で切られる）に余裕を持たせる */
  td.pc{{padding:0 6px 12px 0;vertical-align:top;width:150px}}
  img.pi{{width:150px;height:150px;object-fit:cover;border-radius:8px;display:block;border:1px solid #E5E7EB}}
  .pt{{font-size:10px;color:#6B7280;margin-top:4px;line-height:1.5}}
  .pg{{color:#10B981}}
  .pb{{color:#EF4444}}
  .pf{{color:#9CA3AF}}
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

    <!-- 最上部：APIコストのカード（昨日／今月／1日あたり）。残高・残り日数はオーナー指示 2026-10-03 で外した -->
    {credit_section}

    <!-- 実際の食事写真（オーナー指示 2026-09-22：文章より先に、目で見て分かるように） -->
    {photo_section}

    <!-- 「Claude Code に頼める改善案」はオーナー指示（2026-10-01）でレポートから外した。
         毎回の提案の質が低く、読む時間の無駄になっていたため。戻さないこと。 -->

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

    <!-- 🍬 お菓子・スイーツ・ジュースの特別ルールの適用人数（オーナー依頼 2026-10-07） -->
    {_sweet_rule_section(data.get("sweet_rule"))}

    <!-- ① 日別推移グラフ（利用状況とAPI費用を1枚に統合。オーナー指示 2026-09-30） -->
    <div class="section">
      <h2>📈 日別 利用状況とAPI費用（直近30日）</h2>
      <img class="chart" src="cid:chart_usage" alt="Usage and API Cost Trend">
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
      <img class="chart" src="cid:chart_weight_loss" alt="Weight Loss Progress" style="margin-top:12px">
      <div style="font-size:12px;font-weight:700;color:#6B7280;margin:14px 0 4px">
        減量希望者の「平均Bカウント」と「体重の変化」の相関（Bを抑えている人ほど減っているか・1人1点）
      </div>
      <img class="chart" src="cid:chart_cut_corr" alt="Cut Users B-Count vs Weight Change">
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

    <!-- ④ 減量希望者：成功群 vs 失敗群 -->
    <div class="section">
      <h2>🎯 4. 減量希望者：減量成功群 vs 失敗群</h2>
      <div style="font-size:13px;color:#374151;margin-bottom:8px">
        成功群 <b style="color:#10B981">{cut_n_ok}名</b>（体重が減っている）／
        失敗群 <b style="color:#6B7280">{cut_n_ng}名</b>（減っていない）
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px">
        <tr style="border-bottom:2px solid #E5E7EB;color:#6B7280">
          <th style="padding:8px;text-align:left">1日あたり</th>
          <th style="padding:8px">成功群</th>
          <th style="padding:8px">失敗群</th>
          <th style="padding:8px">差（成功−失敗）</th>
        </tr>{cut_rows}
      </table>
      <img class="chart" src="cid:chart_goal_compare" alt="Cut members: success vs failure">
      <div style="font-size:11px;color:#9CA3AF;margin-top:4px">
        ※ 対象は目標が「減量」の会員。直近60日の体重の傾きがマイナスなら成功群、0以上なら失敗群。
        食事は3食以上記録した日だけを、会員ごとに平均してから比べています（＊＝統計的に有意な差）。{cut_note}
      </div>{rec_html}
    </div>

    <!-- 「5. システム・運用状況」（時間帯別の解析分布・推定コスト）はオーナー指示
         （2026-10-01）でレポートから外した。昨日の推定コストは最上部のクレジット状況に出ている。 -->

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

    # API費用は日別グラフ（chart_usage）にも描くので、グラフより先に取得する
    print("Fetching Claude API credit / cost...")
    try:
        credit = build_credit_info(estimate=fetch_credit_estimate())
    except Exception as e:   # noqa: BLE001 — クレジット取得の失敗でレポートを落とさない
        print(f"  credit info failed: {e}")
        credit = {"status": "error", "message": f"クレジット情報の取得に失敗しました（{e}）。"}
    print(f"  credit status={credit.get('status')} remaining={credit.get('remaining_usd')} "
          f"month={credit.get('spend_month_usd')}")

    print("Generating charts...")
    charts = {
        "chart_usage":    chart_usage(data, credit),
        "chart_weight_loss": chart_weight_loss(data),
        "chart_cut_corr":  chart_cut_corr(data),
        "chart_nutrition": chart_nutrition(data),
        "chart_goal_compare": chart_goal_compare(data),
    }

    print("Fetching member meal photos...")
    photos = fetch_report_photos()
    photo_html = ""
    if photos:
        print(f"  photos: 順調 {len(photos.get('good') or [])}名 / 停滞 {len(photos.get('bad') or [])}名")
        try:
            photo_html = _photo_section(photos, charts)
        except Exception as e:   # noqa: BLE001 — 写真の組み立て失敗でレポートを落とさない
            print(f"  photo section failed: {e}")
            photo_html = ""
    else:
        print("  photos: skipped")

    print("Building HTML email...")
    rdate   = data["report_date"]
    subject = f"[ABダイエット] デイリーレポート {rdate}"
    html    = build_html(data, credit, photo_html)

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
