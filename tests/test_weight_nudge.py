"""進捗タブの「体重を記録すると…」誘導の回帰テスト。

オーナー指示 2026-08：
  進捗タブを開いたとき、その会員の直近7日間に体重記録が1件も無ければ、
  キャラクターの上部に誘導を出し、タップで体重記録へ飛ばす。
  1件でもあれば表示しない。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def test_nudge_is_above_the_character():
    """誘導はキャラクターより上に置くこと（オーナー指示）。"""
    html = _html()
    nudge = html.index('id="weight-nudge"')
    stage = html.index('id="chara-stage"')
    assert nudge < stage, "誘導がキャラクターより下にあります"
    # 進捗タブ（view-chara）の中にあること
    view = html.index('<div id="view-chara"')
    assert view < nudge < stage


def test_nudge_text_matches_instruction():
    html = _html()
    m = re.search(r'id="weight-nudge".*?</div>', html, re.S)
    assert m, "誘導が見つかりません"
    block = m.group(0)
    assert "体重を記録するとグラフが動きます！" in block
    assert "記録する" in block and "⚖️" in block


def test_nudge_is_hidden_by_default():
    """既定は非表示。記録が無いと分かったときだけ出す。"""
    html = _html()
    m = re.search(r'<div id="weight-nudge"([^>]*)>', html)
    assert m and "display:none" in m.group(1), "既定で表示されてしまいます"


def test_nudge_opens_weight_input():
    html = _html()
    m = re.search(r'<div id="weight-nudge"([^>]*)>', html)
    assert 'onclick="openWeightRecord()"' in m.group(1), "タップで体重記録へ飛びません"
    fn = re.search(r"function openWeightRecord\(\)\s*\{(.*?)\n\}", html, re.S)
    assert fn, "openWeightRecord が見つかりません"
    body = fn.group(1)
    assert "switchView('main')" in body, "記録タブへ切り替えていません"
    assert "switchRecordSub('weight')" in body, "体重/日記のサブタブを開いていません"
    assert "weight-input-today" in body, "体重の入力欄にフォーカスしていません"


def test_last_seven_days_are_checked():
    """判定期間は直近7日間（今日を含む）であること。"""
    html = _html()
    assert re.search(r"WEIGHT_NUDGE_DAYS\s*=\s*7", html), "判定日数が7日になっていません"
    fn = re.search(r"function hasRecentWeight\([^)]*\)\s*\{(.*?)\n\}", html, re.S)
    assert fn, "hasRecentWeight が見つかりません"
    body = fn.group(1)
    assert "getWeightLog()" in body, "体重の記録を見ていません"
    # 0 から days-1 まで＝今日を含む7日間
    assert "let i = 0" in body and "i < days" in body


def test_nudge_is_refreshed_on_progress_tab_and_after_save():
    """進捗タブを開いたときと、体重を保存した直後に表示を更新すること。"""
    html = _html()
    chara = re.search(r"function renderChara\(\)\s*\{(.*?)\n\}", html, re.S)
    assert chara and "updateWeightNudge()" in chara.group(1), \
        "進捗タブを開いても判定していません"
    save = re.search(r"function saveTodayWeight\(\)\s*\{(.*?)\n\}", html, re.S)
    assert save and "updateWeightNudge()" in save.group(1), \
        "体重を保存しても誘導が消えません"
