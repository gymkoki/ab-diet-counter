"""減量群と増量群の比較に使う統計処理（外部ライブラリ不要）。

Render の本番環境に SciPy を入れずに済むよう、必要な分だけを純Pythonで実装している。
値の正しさは tests/test_diet_analysis.py で SciPy の計算結果と照合している。

【解析の考え方】
- 比べる単位は「会員」。1人の中の複数日は独立ではないので、日を単位に t 検定すると
  たくさん記録した人の影響が大きくなり、有意差が出やすくなってしまう（見かけの有意差）。
  そこで会員ごとに平均を出してから、その平均どうしを比べる。
- 2群の人数も分散も揃わないため、等分散を仮定しない Welch の t 検定を使う。
- 人数が少ないと p 値だけでは判断を誤るため、効果量（Hedges' g）も必ず出す。
- 複数の項目を同時に検定すると偶然の「有意」が混じるので、Holm 法で補正した p 値で判定する。
"""
import math


def mean(xs):
    return sum(xs) / len(xs)


def sample_var(xs):
    """不偏分散（n-1 で割る）。2件未満は計算できないので None。"""
    n = len(xs)
    if n < 2:
        return None
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - 1)


# ── t 分布の累積確率（正則化不完全ベータ関数から求める）──────────────
def _betacf(a, b, x, max_iter=300, eps=3e-14):
    """不完全ベータ関数の連分数展開（Numerical Recipes の betacf）。"""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c
        c = c if abs(c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c
        c = c if abs(c) > 1e-300 else 1e-300
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a, b, x):
    """正則化不完全ベータ関数 I_x(a, b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_bt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t, df):
    """自由度 df の t 分布で、|T| >= |t| となる確率（両側 p 値）。"""
    if df <= 0 or not math.isfinite(t):
        return None
    return betainc(df / 2.0, 0.5, df / (df + t * t))


def t_quantile(p, df):
    """t 分布の上側 p 点（二分法）。信頼区間の幅を出すのに使う。"""
    lo, hi = 0.0, 1000.0
    for _ in range(200):
        mid = (lo + hi) / 2
        # 片側上側確率 = 両側p / 2
        if t_two_sided_p(mid, df) / 2.0 > p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ── 検定本体 ────────────────────────────────────────────────────
def welch_ttest(a, b):
    """Welch の t 検定（等分散を仮定しない）。a − b の向きで返す。

    どちらかの群が2人未満なら検定できないので None を返す。
    """
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    va, vb = sample_var(a), sample_var(b)
    ma, mb = mean(a), mean(b)
    se2 = va / na + vb / nb
    diff = ma - mb
    if se2 <= 0:
        # 両群とも全員まったく同じ値。差が無ければ p=1、差があれば判定不能扱い。
        return {"t": 0.0, "df": float(na + nb - 2), "p": 1.0 if diff == 0 else None,
                "diff": diff, "ci_low": diff, "ci_high": diff}
    se = math.sqrt(se2)
    t = diff / se
    df = se2 ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    tq = t_quantile(0.025, df)
    return {
        "t": t, "df": df, "p": t_two_sided_p(t, df),
        "diff": diff, "ci_low": diff - tq * se, "ci_high": diff + tq * se,
    }


def hedges_g(a, b):
    """効果量 Hedges' g（小標本の偏りを補正した Cohen's d）。a − b の向き。

    目安：|g| < 0.2 ほぼ差なし／0.2〜 小／0.5〜 中／0.8〜 大。
    """
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    va, vb = sample_var(a), sample_var(b)
    pooled = ((na - 1) * va + (nb - 1) * vb) / (na + nb - 2)
    if pooled <= 0:
        return 0.0
    d = (mean(a) - mean(b)) / math.sqrt(pooled)
    correction = 1.0 - 3.0 / (4.0 * (na + nb) - 9.0)
    return d * correction


def holm(pvalues):
    """Holm 法で補正した p 値（入力と同じ順番で返す）。None はそのまま None。"""
    idx = [i for i, p in enumerate(pvalues) if p is not None]
    m = len(idx)
    out = [None] * len(pvalues)
    running = 0.0
    for rank, i in enumerate(sorted(idx, key=lambda k: pvalues[k])):
        adj = min(1.0, (m - rank) * pvalues[i])
        running = max(running, adj)      # 単調性を保つ
        out[i] = running
    return out


def effect_label(g):
    if g is None:
        return "判定不能"
    a = abs(g)
    if a < 0.2:
        return "ほぼ差なし"
    if a < 0.5:
        return "小さい差"
    if a < 0.8:
        return "中くらいの差"
    return "大きい差"


def slope_per_day(points):
    """[(日数, 値), ...] の最小二乗の傾き（値/日）。2点未満や同日だけなら None。"""
    if len(points) < 2:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
