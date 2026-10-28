# 管理画面「💰 収益を伸ばすために今日やること」のカードの回帰テスト（2026-10-26）。
#
# レポートは朝に作った時点の内容で保存されるため、提案の作成に失敗した日は「空欄のまま」残る
# （再送しても同じ）。管理画面から同じAPIを呼んでその場で作って見られるようにし、
# 失敗したときは理由（detail）を画面に出す（オーナーがその画面を送れば原因が分かる）。
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")


def _html():
    with open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8") as f:
        return f.read()


def test_card_calls_the_same_api_and_shows_reason():
    html = _html()
    assert 'id="revenue-card"' in html and 'onclick="loadRevenueProposals()"' in html
    body = re.search(r"async function loadRevenueProposals\(\) \{.*?\n  \}", html, re.S).group(0)
    assert "fetch('/api/admin/revenue-proposals?wait=20')" in body
    assert "r.status !== 202" in body, "作成中(202)のあいだは問い合わせを繰り返して待つ"
    assert "d.detail" in body, "失敗の理由を画面に出すこと"


def _render(data):
    html = _html()
    fn = re.search(r"  function revenueProposalsHtml\(d\) \{.*?\n  \}", html, re.S).group(0)
    esc = re.search(r"  function escapeHtml\(s\) \{.*?\n  \}", html, re.S).group(0)
    js = f"{esc}\n{fn}\nconsole.log(revenueProposalsHtml({json.dumps(data, ensure_ascii=False)}));"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(js)
        path = tmp.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout
    finally:
        os.unlink(path)


@needs_node
def test_proposals_are_rendered_and_escaped():
    out = _render({"date": "2026-10-26", "cached": False, "proposals": [
        {"title": "<b>お試し</b>の案内", "why": "理由", "effect": "+5,500円/月", "kind": "運営", "prompt": "依頼文A"},
        {"title": "二つ目", "why": "w", "effect": "", "kind": "?", "prompt": "依頼文B"},
    ]})
    assert "1. &lt;b&gt;お試し&lt;/b&gt;の案内" in out and "<b>お試し</b>" not in out
    assert "💴 見込み：+5,500円/月" in out and "依頼文A" in out and "依頼文B" in out
    assert ">運営<" in out and ">アプリ<" in out
    assert "（いま作りました）" in out and "全部まとめてコピー" in out


@needs_node
def test_empty_proposals():
    assert "今日の提案はありませんでした" in _render({"proposals": []})
