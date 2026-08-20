# フロントエンド（templates/index.html 内のJavaScript）の壊れ検知テスト。
# 1ファイルに全コードが入っているため、構文エラーが1つあるだけで
# アプリ全体（文章入力・写真解析など）が動かなくなる。
# ここで <script> の中身を取り出して Node.js の構文チェックにかける。
#
# 実行方法:  pytest tests/ -q  （node が無い環境ではスキップ）

import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "templates", "index.html")


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_index_inline_scripts_have_no_syntax_errors():
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()

    # src属性なしの<script>（インラインJS）を全部連結して構文チェック
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts, "index.html にインラインJSが見つかりません"
    joined = "\n;\n".join(scripts)

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(joined)
        path = tmp.name
    try:
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        assert proc.returncode == 0, f"JavaScriptの構文エラー:\n{proc.stderr}"
    finally:
        os.unlink(path)


def test_critical_functions_exist():
    """文章入力・写真解析まわりの主要関数が消えていないこと（同時開発での消失検知）。"""
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    for name in [
        "submitTextEntry",
        "resumeTextItem",
        "fetchAnalyze",
        "parseAnalyzeResponse",
        "_analyzeFailureMessage",
        "showError",
    ]:
        assert re.search(rf"(async\s+)?function\s+{name}\s*\(", html), f"function {name} が見つかりません"


def test_weight_tab_has_hint_above_input():
    """体重タブ：入力フォームの上のひとこと説明が消えていないこと。
    （見出し → 説明 → 入力欄 の順。控えめなグレー・本文より小さい文字で表示する）"""
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    assert 'class="weight-hint"' in html, "体重入力の説明テキストがありません"
    assert "体重を記録すると、30日間の変化グラフで頑張りが見えます" in html
    assert re.search(r"\.weight-hint\s*\{[^}]*color:var\(--text-muted\)", html), \
        "説明テキストがグレー系（--text-muted）で指定されていません"
    order = [html.index('id="weight-sub-title"'), html.index('class="weight-hint"'),
             html.index('class="weight-inline-row"')]
    assert order == sorted(order), "見出し → 説明 → 入力欄 の並びになっていません"
