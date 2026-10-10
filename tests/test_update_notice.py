"""アプリ内「アップデートお知らせ」の回帰テスト。

方針（CLAUDE.md）：お知らせを出すのはオーナーから明示的に指示があったときだけ。
そのとき No. と NOTICE_KEY を必ずセットで更新する（片方だけ変えると
既存の端末に出ない／同じ内容が二度出る、といった事故になる）。

オーナー指示 2026-08：コツ（TODAY'S TIP）はアプリ起動時ではなく、
写真の栄養解析が終わったタイミングで表示する。お知らせの上には重ねない。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def test_notice_number_and_key_are_in_sync():
    """No.NN と NOTICE_KEY の版番号がずれていないこと。

    NOTICE_KEY を上げ忘れると、既存の会員には新しいお知らせが表示されない。
    """
    html = _html()
    no = re.search(r"最新アップデート No\.(\d+)", html)
    key = re.search(r"NOTICE_KEY\s*=\s*'ab-diet-notice-v(\d+)'", html)
    assert no, "お知らせの No. が見つかりません"
    assert key, "NOTICE_KEY が見つかりません"
    # 版番号の対応は No.NN ↔ v(NN+2)（過去の経緯でずれているが、差は一定に保つ）
    assert int(key.group(1)) - int(no.group(1)) == 2, (
        f"No.{no.group(1)} と NOTICE_KEY v{key.group(1)} の対応がずれています。"
        "お知らせを更新するときは両方を1つずつ上げてください"
    )


def test_notice_body_is_not_empty():
    """本文が空のまま公開されないこと。"""
    html = _html()
    m = re.search(r'最新アップデート No\.\d+</div>\s*<div[^>]*>(.*?)</div>\s*<div[^>]*>（',
                  html, re.S)
    assert m, "お知らせ本文が見つかりません"
    text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    assert len(text) >= 30, "お知らせ本文が短すぎます"


def test_daily_tip_never_covers_the_notice():
    """お知らせが出ている間は、コツをその上に重ねないこと。"""
    html = _html()
    m = re.search(r"function maybeShowDailyTip\(\)\s*\{(.*?)\n\}", html, re.S)
    assert m, "maybeShowDailyTip が見つかりません"
    body = m.group(1)
    assert "update-notice-overlay" in body, \
        "お知らせ表示中にコツを見送る処理が入っていません"


def test_notice_36_asks_for_one_photo_per_meal():
    """お知らせ No.36（オーナー指示 2026-10-03）：
    食材をなるべく1枚にまとめて撮影してほしいこと、その理由（AI料金は写真1枚ごと）を伝える。"""
    html = _html()
    m = re.search(r"最新アップデート No\.(\d+)", html)
    assert m and int(m.group(1)) >= 36
    if int(m.group(1)) != 36:
        return   # 次のお知らせに入れ替わったら、この検査は役目を終える
    body = html[m.start(): html.index("（奥松）", m.start())]
    assert "食材をまとめて1枚の写真に収まるように" in body, "お願いの本文が入っていない"
    assert "写真1枚ごと" in body and "料金" in body, "まとめて撮ってほしい理由（料金）が伝わらない"


def test_notice_37_asks_to_record_everything():
    """お知らせ No.37（オーナー指示 2026-10-10）：
    減量できている方は1日約5回・苦戦中の方は約3回写真をアップしていたこと、
    記録した分しかBカウントが数えられないこと、文章入力でもよいので正確に記録してほしいことを伝える。"""
    html = _html()
    m = re.search(r"最新アップデート No\.(\d+)", html)
    assert m and int(m.group(1)) >= 37
    if int(m.group(1)) != 37:
        return   # 次のお知らせに入れ替わったら、この検査は役目を終える
    body = html[m.start(): html.index("（奥松）", m.start())]
    text = re.sub(r"<[^>]+>", "", body)
    assert "減量できている方は1日に約5回" in text and "苦戦している方は約3回" in text
    assert "記録した分しかBカウントが数えられない" in text
    assert "文章での入力" in text and "正確に" in text
    assert "1枚にまとめて" in text, "No.36（1枚にまとめて撮影）と食い違って読めてしまう"
