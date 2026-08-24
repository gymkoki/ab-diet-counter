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
