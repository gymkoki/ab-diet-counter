# デイリーレポートの「体重が2kg以上ふえた人／へった人」の分け方の回帰テスト。
#
# 経緯（オーナー指摘 2026-09）：
#   実際は痩せているのに、直近で少し増えただけの人が
#   「減量が進んでいない人」として表示されていた。
#   原因は、増減の符号を見ずに「変化が大きい順に上位10名」を載せていたこと。
#   枠が埋まらないと、2kg減った人まで同じ表に出てしまう。
#
# 仕様：プレ（初回記録）→ポスト（最新記録）の体重変化で判定し、
#   ・2kg以上へった人  … 別の表（うまくいっている人の記録のしかた）
#   ・2kg以上ふえた人  … 別の表（要注意・声かけの対象）
#   ・±2kg未満（横ばい）… どちらにも載せない（人数だけ示す）

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client():
    m.init_db()
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c


def _d(back):
    return (datetime.datetime.now(m.JST) - datetime.timedelta(days=back)).strftime("%Y-%m-%d")


def _seed(uid, name, weights, b_days=(1, 2, 3)):
    """減量希望の会員を1人作る。weights = {何日前: 体重}"""
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        for t in ("user_profile", "daily_b_count", "daily_weight", "usage_log"):
            cur.execute(f"DELETE FROM {t} WHERE user_id={m.PH}", (uid,))
        cur.execute(
            f"""INSERT INTO user_profile (user_id, display_name, gender, goal, updated_at)
               VALUES ({m.PH},{m.PH},{m.PH},{m.PH},{m.PH})""",
            (uid, name, "male", "cut", _d(0) + "T08:00:00"))
        for back in b_days:
            cur.execute(
                f"INSERT INTO daily_b_count (user_id, date, b_count, created_at) "
                f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, _d(back), 4.0, _d(back) + "T23:00:00"))
            cur.execute(
                f"INSERT INTO usage_log (user_id, created_at) VALUES ({m.PH},{m.PH})",
                (uid, f"{_d(back)}T12:30:00"))
        for back, w in weights.items():
            cur.execute(
                f"INSERT INTO daily_weight (user_id, date, weight, created_at) "
                f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, _d(back), w, _d(back) + "T08:00:00"))
        conn.commit(); conn.close()


def _cc(client):
    return client.get("/api/admin/report-data",
                      headers={"X-Admin-Password": m.ADMIN_PASSWORD}).get_json()["cut_corr"]


def _names(rows):
    return [r["name"] for r in rows]


# ── オーナーが実際に困ったケース ────────────────────────────────
def test_overall_loser_is_not_listed_as_not_losing(client):
    """【本題】全体では痩せているのに直近だけ少し増えた人を、
    「ふえた人」の表に出さないこと。"""
    # 初回 75kg → 5日前 70kg → 最新 70.2kg（直近は+200gだが、プレ比では -4.8kg）
    _seed("band-real-loser", "実は痩せている人", {60: 75.0, 5: 70.0, 1: 70.2})
    cc = _cc(client)
    assert "実は痩せている人" not in _names(cc["gained_members"]), \
        "全体では痩せている人が「ふえた人」に出ている"
    assert "実は痩せている人" in _names(cc["lost_members"]), \
        "プレ比で2kg以上減っているのに「へった人」に出ていない"


def test_split_uses_first_record_not_recent_days(client):
    """判定はプレ（初回記録）→ポスト（最新記録）で行うこと。"""
    _seed("band-first", "初回比で判定", {90: 80.0, 1: 77.0})
    row = next(r for r in _cc(client)["lost_members"] if r["name"] == "初回比で判定")
    assert row["change_kg"] == -3.0, f"初回との差になっていない: {row}"
    assert row["since"] == _d(90), "初回記録日（プレ）が入っていない"


# ── 分け方そのもの ──────────────────────────────────────────
def test_two_groups_are_separated(client):
    """2kg以上へった人と、2kg以上ふえた人が別々に返ること。"""
    _seed("band-lost", "へった人", {40: 72.0, 1: 69.0})     # -3.0kg
    _seed("band-gained", "ふえた人", {40: 60.0, 1: 62.5})   # +2.5kg
    cc = _cc(client)
    assert "へった人" in _names(cc["lost_members"])
    assert "ふえた人" in _names(cc["gained_members"])
    assert "へった人" not in _names(cc["gained_members"])
    assert "ふえた人" not in _names(cc["lost_members"])


def test_flat_members_are_in_neither_table(client):
    """±2kg未満（横ばい）はどちらの表にも出さず、人数だけ数えること。"""
    _seed("band-flat", "横ばいの人", {40: 70.0, 1: 69.2})   # -0.8kg
    cc = _cc(client)
    assert "横ばいの人" not in _names(cc["lost_members"])
    assert "横ばいの人" not in _names(cc["gained_members"])
    assert cc["band_counts"]["flat"] >= 1, "横ばいの人数が数えられていない"


def test_threshold_is_two_kilos(client):
    """しきい値がちょうど2kgであること（2.0kgは対象、1.9kgは対象外）。"""
    assert _cc(client)["loss_band_kg"] == 2.0
    _seed("band-edge-in", "ちょうど2kg減", {40: 70.0, 1: 68.0})
    _seed("band-edge-out", "1.9kg減", {40: 70.0, 1: 68.1})
    cc = _cc(client)
    assert "ちょうど2kg減" in _names(cc["lost_members"])
    assert "1.9kg減" not in _names(cc["lost_members"])


def test_sign_convention(client):
    """change_kg はマイナスが減量、プラスが増量であること（符号の取り違え防止）。"""
    _seed("band-sign-l", "減った", {40: 80.0, 1: 77.0})
    _seed("band-sign-g", "増えた", {40: 60.0, 1: 63.0})
    cc = _cc(client)
    assert next(r for r in cc["lost_members"] if r["name"] == "減った")["change_kg"] < 0
    assert next(r for r in cc["gained_members"] if r["name"] == "増えた")["change_kg"] > 0


def test_ordering(client):
    """へった人はよく減った順、ふえた人はよく増えた順に並ぶこと。"""
    _seed("band-l1", "減2", {40: 70.0, 1: 67.5})    # -2.5
    _seed("band-l2", "減5", {40: 70.0, 1: 65.0})    # -5.0
    _seed("band-g1", "増2", {40: 60.0, 1: 62.5})    # +2.5
    _seed("band-g2", "増5", {40: 60.0, 1: 65.0})    # +5.0
    cc = _cc(client)
    lost = [r for r in cc["lost_members"] if r["name"].startswith("減")]
    gained = [r for r in cc["gained_members"] if r["name"].startswith("増")]
    assert lost == sorted(lost, key=lambda r: r["change_kg"]), "へった人の並びが逆"
    assert gained == sorted(gained, key=lambda r: -r["change_kg"]), "ふえた人の並びが逆"


def test_needs_two_weight_records(client):
    """体重が1回しか無い人は、プレとポストを比べられないので対象外。"""
    _seed("band-one", "体重1回だけ", {1: 70.0})
    cc = _cc(client)
    assert "体重1回だけ" not in _names(cc["lost_members"])
    assert "体重1回だけ" not in _names(cc["gained_members"])


# ── レポート側 ────────────────────────────────────────────────
def test_report_has_two_separate_charts():
    """メールに2つのグラフが別々に載っていること。"""
    with open(os.path.join(ROOT, "report", "send_report.py"), encoding="utf-8") as f:
        src = f.read()
    assert "def chart_gained_slots" in src and "def chart_lost_slots" in src
    assert "cid:chart_gained_slots" in src and "cid:chart_lost_slots" in src
    # 1枚にまとめていた旧グラフは残さない
    assert "chart_no_loss_slots" not in src


def test_report_explains_the_basis():
    """「初回記録との比較である」ことを本文に明記すること
    （直近の増減と誤解されると、また同じ指摘になる）。"""
    with open(os.path.join(ROOT, "report", "send_report.py"), encoding="utf-8") as f:
        src = f.read()
    assert "初回記録（プレ）→最新記録（ポスト）" in src
    assert "横ばい" in src, "どちらにも出ない人がいることの説明がありません"
