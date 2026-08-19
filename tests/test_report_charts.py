"""デイリーレポートに載せるグラフの回帰テスト。

・メール本文が参照している画像(cid:)と、実際に添付する画像が一致していること。
  ずれると受信側で「画像が表示されない」空枠になる。
・オーナーが「使わないので外して」と指示したグラフが復活していないこと。
  （2026-08：Bカウント推移／体重推移の「個人＋集団平均」の2枚を廃止）
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "report", "send_report.py")


def _src():
    with open(SRC, encoding="utf-8") as f:
        return f.read()


def _referenced_cids(src):
    """メール本文のHTMLが参照している画像ID。"""
    return set(re.findall(r'src="cid:(chart_\w+)"', src))


def _attached_cids(src):
    """main() で実際に生成・添付している画像ID。"""
    m = re.search(r"charts = \{(.*?)\n    \}", src, re.S)
    assert m, "charts の定義が見つかりません"
    ids = set(re.findall(r'"(chart_\w+)":', m.group(1)))
    # 条件付きで足される画像も拾う（例：クレジット状況）
    ids |= set(re.findall(r'charts\["(chart_\w+)"\]', src))
    return ids


def test_every_referenced_chart_is_attached():
    """本文が参照しているのに添付していない画像が無いこと（＝空枠にならない）。"""
    src = _src()
    missing = _referenced_cids(src) - _attached_cids(src)
    assert not missing, f"本文が参照しているのに添付されていない画像があります: {sorted(missing)}"


def test_no_orphan_charts_are_generated():
    """本文に出さない画像を無駄に生成していないこと（メールが重くなる）。"""
    src = _src()
    orphans = _attached_cids(src) - _referenced_cids(src)
    assert not orphans, f"本文で使われていない画像を生成しています: {sorted(orphans)}"


def test_removed_charts_stay_removed():
    """オーナー指示で外した「個人＋集団平均」の2枚が復活していないこと。"""
    src = _src()
    # chart_weight_loss と紛れないよう、名前は完全一致で判定する
    referenced = _referenced_cids(src)
    attached = _attached_cids(src)
    for name in ("chart_b_count", "chart_weight"):
        assert f"def {name}(" not in src, f"{name} が復活しています（オーナー指示で廃止）"
        assert name not in referenced, f"{name} がメール本文に復活しています"
        assert name not in attached, f"{name} が添付画像に復活しています"
    # ※ 減量進捗（初回体重比）は別のグラフなので残す。廃止したのは上の2枚だけ。
    assert "Bカウント推移（直近30日）" not in src
    assert "体重推移（直近30日）" not in src


def test_kept_charts_are_still_there():
    """残すことになっているグラフまで消えていないこと。"""
    src = _src()
    for name in ("chart_usage", "chart_hourly", "chart_weight_loss",
                 "chart_cut_corr", "chart_nutrition", "chart_goal_compare"):
        assert f"def {name}(" in src, f"{name} が消えています"
        assert f"cid:{name}" in src, f"{name} が本文から消えています"
