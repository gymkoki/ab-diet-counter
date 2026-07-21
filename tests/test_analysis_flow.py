# 写真解析フローの回帰テスト。
# 方針（オーナー指示 2026-07）：
#   - 事前チェック（Haiku）を使わず、初手から高精度AI（Sonnet）で1回だけ解析する。
#   - 大盛り（主食・肉魚）と油は「厳しめ（多め）」に見積もり、確認質問はしない。
# 同時並行の開発で、うっかり二段階（Haiku事前チェック→Sonnet）に戻さないための保険。

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "templates", "index.html")
APP = os.path.join(ROOT, "app.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_client_analyze_is_single_pass_no_precheck():
    """写真解析の本体 analyzeItem が、事前チェック(/precheck)を呼ばず1回で解析すること。"""
    html = _read(INDEX)
    m = re.search(r"async function analyzeItem\(.*?\n\}", html, re.S)
    assert m, "analyzeItem が見つかりません"
    body = m.group(0)
    # 事前チェック（Haiku）や解析後の再計算（二重計算）を本フローで呼ばない
    assert "collectPrecheckAnswers" not in body, "analyzeItem が事前チェック(Haiku)を呼んでいます（1回解析に戻すこと）"
    assert "collectClarifyAnswers" not in body, "analyzeItem が解析後の再確認→再計算をしています（二重計算になる）"
    assert "reanalyzeWithAnswers" not in body, "analyzeItem が確認回答での再計算をしています（二重計算になる）"
    # /analyze は1回だけ叩く
    assert body.count("fetchAnalyze('/analyze'") == 1


def test_analysis_prompt_has_strict_estimation_rules():
    """ANALYSIS_PROMPT に『厳しめ（多め）に見積もる』方針が入っていること。"""
    src = _read(APP)
    for kw in ["厳しめ", "過小評価", "大盛り", "調理油"]:
        assert kw in src, f"ANALYSIS_PROMPT に『{kw}』の指示が見当たりません"


def test_analysis_prompt_no_clarify_output_field():
    """本解析の出力JSONから clarify フィールドが外れていること（確認質問をしないため）。"""
    src = _read(APP)
    # 出力スキーマ内の clarify フィールド定義が無いこと
    assert '"clarify": {' not in src, "出力JSONに clarify フィールドが残っています（確認質問は廃止）"
