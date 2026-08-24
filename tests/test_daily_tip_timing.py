# 「今日のABダイエットのコツ」を出すタイミングの回帰テスト。
#
# オーナー指示 2026-08：
#   アプリを開いた瞬間ではなく、各写真の栄養解析が終わったタイミングで毎回表示する。

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def _fn(name):
    """指定した関数の中身だけを取り出す。"""
    html = _html()
    m = re.search(r"(?:async )?function " + name + r"\(.*?\n\}", html, re.S)
    assert m, f"{name} が見つかりません"
    return m.group(0)


def test_tip_is_shown_after_photo_analysis():
    """写真の解析が終わったらコツを表示すること。"""
    body = _fn("analyzeItem")
    assert "maybeShowDailyTip" in body, "解析後にコツを出す処理がありません"


def test_tip_is_not_shown_when_analysis_failed():
    """解析に失敗したときは出さないこと（エラーの案内を隠さない）。"""
    body = _fn("analyzeItem")
    m = re.search(r"if \(analyzed\) setTimeout\(maybeShowDailyTip", body)
    assert m, "成功したときだけ出す形になっていません"
    # analyzed は「結果があり、かつエラーでない」で決まること
    assert re.search(r"const analyzed = !!\(data && !data\.error\)", body)


def test_tip_is_not_shown_on_app_start():
    """アプリ起動時には出さないこと（オーナー指示で廃止）。"""
    html = _html()
    assert "_step(maybeShowDailyTip" not in html, "起動時の表示が残っています"


def test_tip_is_not_shown_on_returning_to_screen():
    """他アプリから戻ってきただけでは出さないこと。"""
    html = _html()
    # 画面復帰でサーバーと同期する処理（ページ全体のリスナー）だけを見る。
    # ※通信中の中断待ち(_waitUntilVisible)にも同名のイベントがあるので混同しないこと。
    m = re.search(r"document\.addEventListener\('visibilitychange', \(\) => \{.*?\n\}\);",
                  html, re.S)
    assert m, "画面復帰時の処理が見つかりません"
    assert "maybeShowDailyTip" not in m.group(0), "画面復帰で出す処理が残っています"


def test_tip_is_not_shown_after_closing_the_notice():
    """アップデートお知らせを閉じた直後にも出さないこと。"""
    body = _fn("closeUpdateNotice")
    assert "maybeShowDailyTip" not in body, "お知らせを閉じた直後の表示が残っています"


def test_only_one_trigger_remains():
    """コツを出す場所は「解析が終わったとき」の1か所だけであること。"""
    html = _html()
    calls = re.findall(r"maybeShowDailyTip", html)
    # 定義1回 + analyzeItem からの呼び出し1回
    assert len(calls) == 2, f"呼び出し箇所が想定と違います: {len(calls)}箇所"


def test_tip_does_not_replace_itself_while_open():
    """複数枚まとめて解析したとき、読んでいる最中に中身が入れ替わらないこと。"""
    body = _fn("maybeShowDailyTip")
    assert "getComputedStyle(tipEl).display !== 'none'" in body, \
        "すでに表示中なら何もしない、という処理がありません"


def test_tip_never_covers_other_dialogs():
    """運営からの返信など他のお知らせの上には重ねないこと。"""
    body = _fn("maybeShowDailyTip")
    for overlay in ("update-notice-overlay", "reply-notice-overlay",
                    "coach-msg-overlay", "chara-intro-overlay"):
        assert overlay in body, f"{overlay} の確認が抜けています"


def test_tip_history_is_kept():
    """同じコツが繰り返し出ないよう、表示済みの記録は残し続けること。"""
    html = _html()
    assert "TIP_HISTORY_KEY" in html
    body = _fn("_pickDailyTipIndex")
    assert "hist.push(idx)" in body
    assert "if (hist.length >= total) hist = []" in body, "全件出たあとに最初へ戻す処理がありません"
