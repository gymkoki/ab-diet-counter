# デイリーレポートの並び順の回帰テスト。
# オーナー指示（2026-08）：昨日の推定コストとクレジット残高はレポートの一番上に載せる。

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(ROOT, "report", "send_report.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_credit_section_is_placed_first_in_body():
    """クレジット状況が本文の最初のブロックに置かれていること。"""
    src = _read(REPORT)
    body_at = src.index('<div class="body">')
    credit_at = src.index("{credit_section}", body_at)
    coach_at = src.index("{_coach_section(coach_advice)}", body_at)
    stats_at = src.index("1. 利用統計", body_at)
    assert credit_at < coach_at, "クレジット状況がAI減量コーチより下にあります"
    assert credit_at < stats_at, "クレジット状況が利用統計より下にあります"


def test_credit_section_appears_only_once():
    """移動漏れで二重に表示されないこと。"""
    src = _read(REPORT)
    assert src.count("{credit_section}") == 1, "クレジット状況が複数箇所に出ています"


def test_credit_heading_has_no_stale_number():
    """先頭に移動したので「6.」の通し番号が残っていないこと。"""
    src = _read(REPORT)
    assert "6. Claude API クレジット状況" not in src, "古い通し番号が残っています"
    assert "Claude API クレジット状況" in src


def test_cross_reference_points_upward():
    """システム欄からの参照が「上の」になっていること。"""
    src = _read(REPORT)
    assert "下の「6. Claude API クレジット状況」を参照" not in src
    assert "上の「Claude API クレジット状況」を参照" in src


def test_credit_section_shows_yesterday_cost_and_balance():
    """昨日の推定コストとクレジット残高の両方が載っていること。"""
    src = _read(REPORT)
    start = src.index("def _credit_section")
    block = src[start:start + 6000]
    assert "昨日の推定コスト" in block
    assert "クレジット残高" in block
