# コピーご飯の複数選択（オーナー要望 2026-10-03）の回帰テスト。
# 「日によって組み合わせが違うので、1品ずつ選ぶと朝だけで2回記録したことになり違和感がある」
# → 選択画面で複数を選び、まとめて「1回の食事」（記録1件）として追加する。

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "templates", "index.html")


def _read():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def _func(html, name):
    m = re.search(r"function " + name + r"\(.*?\n\}", html, re.S)
    assert m, f"{name} が見つかりません"
    return m.group(0)


def test_picker_toggles_instead_of_adding_immediately():
    """一覧のタップは「選択の切り替え」で、その場で追加しないこと。"""
    body = _func(_read(), "openCopyPicker")
    assert "toggleCopyPick(" in body
    assert "addCopyMealToRecord(" not in body


def test_selected_meals_become_one_record():
    """まとめて追加しても記録は1件だけ増えること（＝1回の食事として数える）。"""
    body = _func(_read(), "addSelectedCopyMeals")
    assert body.count(".items.push(") == 1, "選んだ品ごとに記録が増えています"
    assert "mergeCopyResults(picked)" in body


def test_merge_sums_b_and_nutrition():
    body = _func(_read(), "mergeCopyResults")
    for key in ("total_b_count", "total_protein_g", "total_veg_g", "foods"):
        assert key in body


def test_add_button_exists():
    html = _read()
    assert 'id="copy-add-btn"' in html
    assert "1回の食事" in html
