# 記録画面の「記録方法カード」の回帰テスト（オーナー指示 2026-08）。
#
# 経緯：「Bを手動追加」は、Bを選ぶ処理(toggleOilMenu / saveOil)は残っていたのに
#       画面から呼び出す場所が消えており、実質使えない状態だった。
#       写真・文章・コピーご飯・Bだけ追加の4つを、同じ大きさ・同じデザインの
#       カードとして並べ、どれも埋もれないようにする。

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "templates", "index.html")


def _html():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def _panel():
    """buildPanel が組み立てる記録画面のHTML部分だけを取り出す。"""
    html = _html()
    return re.search(r"function buildPanel\(m\) \{.*?\n\}", html, re.S).group(0)


def test_four_record_cards_exist():
    """記録方法が4つとも同じカード(.rec-card)で並んでいること。"""
    panel = _panel()
    assert panel.count('class="rec-card"') == 4, "記録方法のカードが4つになっていない"
    for label in ("写真を追加", "文章で手動入力", "コピーご飯を追加", "Bだけ追加"):
        assert label in panel, f"「{label}」のカードが無い"


def test_b_only_button_label():
    """ラベルが「🍙 Bだけ追加（お菓子・ドリンクなど）」であること。"""
    panel = _panel()
    assert "🍙" in panel, "おにぎりの絵文字が無い"
    m = re.search(r'<button class="rec-card" onclick="toggleOilMenu\([^)]*\)">(.*?)</button>',
                  panel, re.S)
    assert m, "Bだけ追加のカードが見つからない"
    body = m.group(1)
    assert "🍙" in body and "Bだけ追加" in body
    assert "お菓子・ドリンクなど" in body


def test_b_only_button_has_an_entry_point():
    """【重要】Bだけ追加が画面から呼び出せること。
    以前は関数だけ残って呼び出し元が無く、機能が使えなくなっていた。"""
    html = _html()
    assert html.count("toggleOilMenu(") >= 2, "toggleOilMenu を呼ぶ場所が無い（機能が使えない）"
    assert "onclick=\"toggleOilMenu(" in html


def test_b_only_menu_markup_exists():
    """Bを選ぶ欄（B0.5/B1/B2・メモ・追加ボタン）が実際に描画されること。"""
    panel = _panel()
    for needed in ("oilmenu-${m.id}", "oilopt-${m.id}", "oilnote-${m.id}", "oilsave-${m.id}"):
        assert needed in panel, f"{needed} が描画されていない"
    assert "saveOil(" in panel


def test_cards_are_the_same_size():
    """4つのカードが同じ大きさ・同じ見た目になるCSSがあること。"""
    html = _html()
    grid = re.search(r"\.rec-grid \{([^}]*)\}", html).group(1)
    card = re.search(r"\.rec-card \{([^}]*)\}", html).group(1)
    assert "grid-template-columns:1fr 1fr" in grid.replace(" ", " ")
    assert "grid-auto-rows:1fr" in grid, "行の高さが揃わない（カードの大きさがずれる）"
    assert "min-height:120px" in card, "カードが小さいとタップしにくい"


def test_manual_submenu_is_gone():
    """「手動追加」を押してから選ぶ中間メニューは廃止されていること。"""
    html = _html()
    assert "manualmenu-" not in html, "中間メニューが残っている"
    assert "function toggleManualMenu" not in html


def test_only_one_input_opens_at_a_time():
    """文章入力とBだけ追加が同時に開かないこと。"""
    html = _html()
    assert "function closeMealInputs" in html
    body = re.search(r"function toggleOilMenu\(mealId\) \{.*?\n\}", html, re.S).group(0)
    assert "closeMealInputs" in body
    body2 = re.search(r"function chooseManualText\(mealId\) \{.*?\n\}", html, re.S).group(0)
    assert "closeMealInputs" in body2


def test_photo_card_keeps_drag_and_drop():
    """写真カードはドラッグ＆ドロップ用のidを保ったままであること。"""
    panel = _panel()
    assert 'id="add-${m.id}"' in panel
    assert "dragover" in panel and "drop" in panel
