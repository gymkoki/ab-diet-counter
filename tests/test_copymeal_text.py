# コピーご飯（文章登録）をAI自動計算に変更したことの回帰テスト。
# オーナー指示 2026-07：文章で登録するときは、Bカウントをダイヤルで選ばず
# AIが自動認識し、タンパク質・野菜も推測して普段の栄養計算に反映する。

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "templates", "index.html")


def _read():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def test_copymeal_text_uses_ai_not_dial():
    """saveCopyMealText が /analyze-text でAI解析し、ダイヤルのBを使わないこと。"""
    html = _read()
    m = re.search(r"async function saveCopyMealText\(\)\s*\{.*?\n\}", html, re.S)
    assert m, "saveCopyMealText が見つかりません（asyncのAI版になっていない）"
    body = m.group(0)
    assert "fetchAnalyze('/analyze-text'" in body, "文章登録がAI解析(/analyze-text)を呼んでいない"
    assert "_copyBCount" not in body, "ダイヤルのBカウント(_copyBCount)をまだ使っている"
    # 保存する result は解析結果(data)そのもの（タンパク質・野菜を含む）
    assert "result: data" in body


def test_copymeal_text_form_has_no_dial():
    """設定の文章登録フォームからBカウントのダイヤルUIが消えていること。"""
    html = _read()
    # コピーご飯フォーム内に drum-copyb ダイヤル要素が無い
    assert 'id="drum-copyb"' not in html, "Bカウントのダイヤル(drum-copyb)がまだHTMLに残っている"
    # 自動計算の案内文がある
    assert "自動で計算" in html
