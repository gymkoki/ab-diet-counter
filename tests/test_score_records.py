# 採点で「記録の回数」も B と同じ重みで評価する回帰テスト（オーナー指示 2026-11-24：
# 「基本的にBカウントに加えて、1日当たりの食事記録の回数も同じくらい評価される（たくさん記録した方が良い）ように。
#   大幅にUIは変更せずに微修正で」）。数えるのは記録の回数（写真・文章・コピーご飯。写真の枚数だけではない）。
#   ・アプリの採点：B・記録・タンパク質・野菜の4項目の平均（記録は1日 RECORD_TARGET_PER_DAY 回で100%）
#   ・レポートの会員カード：名前の横に「評価 ◎○△（B＋記録）」（Bと記録を半々）
import os
import sys

import pytest

from test_chara_min_meals import PERFECT, _html, _run_score, needs_node

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "report"))


@needs_node
def test_more_records_score_higher():
    res = _run_score([{**PERFECT, "mealCount": n} for n in (3, 4, 6)])
    assert [r["rPct"] for r in res] == [75, 100, 100]
    assert res[0]["score"] < res[1]["score"] == res[2]["score"] == 100


@needs_node
def test_records_weigh_the_same_as_b():
    """Bを超えた日（B 50%）と、記録が2回分足りない日（記録 50%）は、ほかが同じなら同じ点の重み。"""
    over_b, few_rec = _run_score([
        {"foodTotal": 9, "protein": 80, "veg": 350, "exBc": 0, "mealCount": 4},   # B 50%・記録 100%
        {"foodTotal": 4, "protein": 80, "veg": 350, "exBc": 0, "mealCount": 2},   # B 100%・記録 50%
    ])
    assert over_b["bPct"] == 50 and over_b["rPct"] == 100
    assert few_rec["bPct"] == 100 and few_rec["rPct"] == 50
    # どちらも危機管鳥の上限（Bオーバー／2回以下）が別にかかるので、上限をかける前の考え方として同じ
    assert over_b["score"] == few_rec["score"] == 29


def test_breakdown_shows_record_chip_and_advice():
    src = _html()
    assert "📝 記録 ${s.rPct == null ? 100 : s.rPct}%" in src
    assert "RECORD_TARGET_PER_DAY = 4" in src
    body = src[src.index("function charaAdvice"):src.index("function charaAdvice") + 2500]
    assert "間食や飲み物も漏れなく記録すると" in body


def _sr():
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report
    return send_report


def _m(avg_b, target, rpd):
    return {"avg_b_total": avg_b, "b_target": target, "record_rate": {"records_per_day": rpd}}


def test_report_badge_weighs_b_and_records_equally():
    sr = _sr()
    assert sr.RECORD_TARGET_PER_DAY == 4
    assert "評価 ◎" in sr._overall_badge(_m(1.7, 4, 3.8)), "Bが目標以内・記録3.8回（スクショの会員）"
    assert "評価 ○" in sr._overall_badge(_m(5.0, 4, 4.0)), "Bを超えても記録が多ければ ○"
    assert "評価 △" in sr._overall_badge(_m(2.0, 4, 1.5)), "Bが少なくても記録が少なければ △"
    assert sr._overall_badge({"avg_b_total": 2.0, "b_target": 4}) == "", "記録の数字が無ければ出さない"


def test_report_card_shows_badge(monkeypatch):
    sr = _sr()
    monkeypatch.setattr(sr, "_decode_photo", lambda src: b"\x89PNG")
    member = {"name": "おおた", "avg_b_total": 1.7, "b_target": 4, "total_days": 148,
              "photos": [{"src": "x", "date": "2026-11-20", "b_count": 1}],
              "record_rate": {"days": 148, "record_days": 120, "records": 456, "records_per_day": 3.8}}
    html = sr._photo_card(member, "t", {}, False)
    assert "評価 ◎（B＋記録）" in html
    src = open(sr.__file__, encoding="utf-8").read()
    assert "通算平均Bと1日の記録回数を同じ重みで見たもの" in src
