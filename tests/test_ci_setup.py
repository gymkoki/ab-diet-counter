"""CI（GitHub Actions）の設定の回帰テスト。

経緯（2026-10-07）：レポート（report/send_report.py）を import するテストが追加されたが、
CI はレポートの依存（requests・matplotlib など）を入れていなかった。
そのため CI だけで収集エラーになり、テストが1件も実行されずに「tests 失敗」の通知が届いた
（手元には依存が入っていたので気づけなかった）。
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_ci_installs_report_dependencies():
    """CI がレポートの依存を入れていること（外すとレポート系テストが動かなくなる）。"""
    with open(os.path.join(ROOT, ".github", "workflows", "test.yml"), encoding="utf-8") as f:
        wf = f.read()
    assert "pip install -r requirements.txt" in wf
    assert "pip install -r report/requirements.txt" in wf, \
        "CI がレポートの依存を入れていない（レポートを import するテストが CI で収集エラーになる）"


def test_report_tests_do_not_crash_collection():
    """レポートを import するテストは、依存が無い環境でも収集エラーにしないこと。
    import より前に pytest.importorskip で守る（他のレポート系テストと同じ書き方）。"""
    tests_dir = os.path.join(ROOT, "tests")
    offenders = []
    for name in sorted(os.listdir(tests_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(tests_dir, name), encoding="utf-8") as f:
            lines = f.read().splitlines()
        for i, line in enumerate(lines):
            # 関数の中ではなく、ファイルの一番外側で send_report を import している行
            if line.startswith("import send_report") or line.startswith("from send_report"):
                guarded = any("importorskip" in prev for prev in lines[:i])
                if not guarded:
                    offenders.append(f"{name}:{i + 1}")
    assert not offenders, (
        "send_report をファイル先頭で import する前に pytest.importorskip がありません: "
        + ", ".join(offenders)
    )
