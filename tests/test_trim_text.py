# 画面の文字を減らす整理（オーナー指示 2026-11-11：「文字数が多くなってきて見にくい」）。
# 使われている機能は残し、説明文だけを短くした。動いていない部品（かんたん確認・リセット確認）は削除した。
# 長い説明文や、どこからも開かれない部品が戻ってこないことを確かめる。
import pathlib
import re

HTML = (pathlib.Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text(encoding="utf-8")


def _bar(bar_id):
    return re.search(rf'<div id="{bar_id}".*?\n  </div>', HTML, re.S).group(0)


def test_special_rule_bars_are_two_lines():
    """特別ルールのバーは「適用！」と「合計」の2行だけ。ルールの説明の3行目は出さない。"""
    for bar_id in ("sweet-bonus-bar", "sushi-bonus-bar", "juice-bonus-bar"):
        bar = _bar(bar_id)
        assert "特別ルール適用！" in bar
        assert bar.count("<br>") == 1, f"{bar_id} が3行に戻っている"
    assert "全部のシャリで数えます" not in HTML
    assert "1日の合計で数えます" not in HTML


def test_long_explanations_stay_short():
    for long_text in (
        "右下の「設定」から身長・体重を入力してください",
        "記録画面の「コピーご飯を追加」から写真なしでワンタップ登録できます",
        "ダイヤルでの手入力は不要です",
        "見たり編集したりできるようにします",
        "などの修正希望をお送りください。運営に届きます",
        "今日は記録の途中なので含めません",
        "Bは最重要項目なので達成率は50%扱い",
        "どちらかが増えるまでBカウントの達成率は加点されません",
    ):
        assert long_text not in HTML, f"長い説明文が戻っている：{long_text}"


def test_dead_parts_are_removed():
    """開く処理が無い「かんたん確認」と、画面の無い「リセット確認」は残さない。"""
    for dead in ('id="clarify-overlay"', "btn-clarify", "reset-overlay",
                 "function confirmReset", "function doReset"):
        assert dead not in HTML, f"使われていない部品が残っている：{dead}"
