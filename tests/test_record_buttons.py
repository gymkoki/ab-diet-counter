# 記録画面の「記録方法カード」の回帰テスト（オーナー指示 2026-08）。
#
# 経緯：写真・文章・コピーご飯を、同じ大きさ・同じデザインのカードとして並べ、
#       どれも埋もれないようにする（「手動追加」の中間メニューは廃止済み）。
#
# 【変更 2026-08】オーナー指示により「Bだけ追加」を廃止した。
#   入口・選択欄・追加処理は削除したが、**過去に追加済みの記録(isOil)は
#   今までどおり表示・集計する**。ここを壊すと会員の過去の記録が消えるため、
#   下の test_existing_manual_b_records_still_render で守っている。

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


def _panel_markup():
    """_panel() からコメントを除いた、実際に描画される部分だけ。
    廃止した機能の名前は説明コメントに出てよいので、判定はコメント抜きで行う。"""
    body = re.sub(r"<!--.*?-->", "", _panel(), flags=re.S)
    return re.sub(r"^\s*//.*$", "", body, flags=re.M)


def test_three_record_cards_exist():
    """記録方法が3つとも同じ種類のカード(.rec-card)で並んでいること。"""
    panel = _panel()
    assert panel.count('class="rec-card') == 3, "記録方法のカードが3つになっていない"
    for label in ("写真を追加", "文章で手動入力", "コピーご飯を追加"):
        assert label in panel, f"「{label}」のカードが無い"


def test_b_only_button_is_gone():
    """「Bだけ追加」が記録画面から無くなっていること（オーナー指示 2026-08）。"""
    markup = _panel_markup()
    assert "Bだけ追加" not in markup, "「Bだけ追加」のカードが残っている"
    assert "お菓子・ドリンクなど" not in markup
    # 入口も処理も残さない（呼べない関数が残っているとバグの温床になる）
    html = re.sub(r"^\s*//.*$", "", _html(), flags=re.M)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    for gone in ("toggleOilMenu", "selectOil(", "updateOilMenuUI", "saveOil(",
                 "OIL_OPTIONS", "oilmenu-", "oilopt-", "oilsave-"):
        assert gone not in html, f"{gone} が残っている"


def test_existing_manual_b_records_still_render():
    """【重要】過去に「Bだけ追加」で入れた記録(isOil)は、今までどおり
    表示・保存されること。機能を消しても会員の過去の記録は消さない。"""
    html = _html()
    # 一覧のアイコン表示に isOil の分岐と、おにぎりアイコンが残っている
    assert "item.isOil" in html, "過去のBだけ追加の記録が表示できなくなっている"
    assert "function onigiriIcon" in html
    # 保存対象からも外していない（同期・再読み込みで消えない）
    payload = re.search(r"function buildMealsPayload\(includePending\) \{.*?\n\}", html, re.S).group(0)
    assert "isOil" in payload and "oilLabel" in payload


def test_cards_are_big_enough_to_tap():
    """どのカードも指で押しやすい大きさであること。"""
    html = _html()
    grid = re.search(r"\.rec-grid \{([^}]*)\}", html).group(1)
    card = re.search(r"\.rec-card \{([^}]*)\}", html).group(1)
    assert "grid-template-columns:1fr 1fr" in grid
    assert "min-height:120px" in card, "カードが小さいとタップしにくい"


def test_photo_card_is_the_main_action():
    """「写真を追加」が横幅いっぱいの主ボタンであること。

    起動時に写真の選択シートを自動で開くことはブラウザが許さないため
    （タップ操作が必須）、「開いてすぐ1タップで選べる」ようにこのボタンを
    一番上の大きな主ボタンにしている。小さく戻さないこと。"""
    panel = _panel()
    m = re.search(r'<div class="rec-card rec-primary"[^>]*onclick="openFileInput', panel)
    assert m, "写真を追加が主ボタン(rec-primary)になっていない"
    # 主ボタンは4つの中で最初に置く（開いて最初に目に入る位置）
    assert panel.index("rec-primary") < panel.index("chooseManualText")
    html = _html()
    css = re.search(r"\.rec-card\.rec-primary \{([^}]*)\}", html).group(1)
    assert "grid-column:1 / -1" in css, "横幅いっぱいになっていない"
    assert "min-height:132px" in css, "他のカードより大きくなっていない"


def test_manual_submenu_is_gone():
    """「手動追加」を押してから選ぶ中間メニューは廃止されていること。"""
    html = _html()
    assert "manualmenu-" not in html, "中間メニューが残っている"
    assert "function toggleManualMenu" not in html


def test_only_one_input_opens_at_a_time():
    """入力欄を開くときは、先に他の欄を閉じること。"""
    html = _html()
    assert "function closeMealInputs" in html
    for fn in ("chooseManualText", "chooseManualCopy"):
        body = re.search(rf"function {fn}\(mealId\) \{{.*?\n\}}", html, re.S).group(0)
        assert "closeMealInputs" in body, f"{fn} が他の欄を閉じていない"


def test_photo_card_keeps_drag_and_drop():
    """写真カードはドラッグ＆ドロップ用のidを保ったままであること。"""
    panel = _panel()
    assert 'id="add-${m.id}"' in panel
    assert "dragover" in panel and "drop" in panel
