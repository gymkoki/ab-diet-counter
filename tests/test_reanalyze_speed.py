# 「計算し直す」(/reanalyze) が遅くて訂正が反映されない障害の回帰テスト。
#
# 経緯（オーナー報告 2026-08-18）：
#   会員が食事の項目に「白米→75g／鶏天→山芋天3本／焼き鳥2本→ねぎま塩・皮塩」と
#   何度も訂正を入れたのに、「解析に時間がかかる」「なかなか反映されない」。
#
# 原因：写真解析(/analyze)から検索を外したときと同じ問題が /reanalyze に残っていた。
#   訂正の再計算では Web検索ツールを“常に”渡していたため、AIは家庭料理・居酒屋メニューの
#   訂正でも検索を始め、1回の再計算でAPIとのやり取りが2〜4往復に増えていた。
#   サーバーの持ち時間(90秒)を使い切ると会員には「通信エラー」だけが残り、訂正が消える。
#
# 対策：①市販商品・店名を含む訂正のときだけ検索する
#      ②それ以外は写真解析と同じ持ち時間（1回40秒・合計45秒）で必ず返す
#      ③訂正が複数書かれていても全て反映させる（プロンプト）

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def client(monkeypatch):
    m.init_db()
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c


def _capture(monkeypatch):
    captured = {}

    def fake_create_and_parse(_client, **kwargs):
        captured.update(kwargs)
        return {"foods": []}

    monkeypatch.setattr(m, "create_and_parse", fake_create_and_parse)
    monkeypatch.setattr(m, "get_client", lambda: object())
    return captured


# ── ① 検索するかどうかの判定 ────────────────────────────────────
@pytest.mark.parametrize("correction", [
    "白米→75g",
    "鶏天→山芋天3本",
    "焼き鳥2本→ねぎま塩・皮塩",
    "白米→75g、鶏天→山芋天3本、焼き鳥2本→ねぎま塩・皮塩",
    "ご飯は茶碗半分です",
    "豆腐ではなくヨーグルトです",
])
def test_home_cooking_corrections_do_not_search(correction):
    """家庭料理・居酒屋メニューの訂正では検索しない（検索しても得るものが無い）。"""
    assert m._correction_needs_web_search(correction) is False, correction


@pytest.mark.parametrize("correction", [
    "これはセブンのチョコバナナアイスです",
    "ローソンのブランパンです",
    "市販のプロテインバーです",
    "カロリーメイト2本です",
    "この商品の栄養成分で計算してください",
])
def test_product_corrections_still_search(correction):
    """商品名・店名での訂正は、実測カロリーを調べる価値があるので検索を許す。"""
    assert m._correction_needs_web_search(correction) is True, correction


# ── ② 実際の /reanalyze に渡る持ち時間 ──────────────────────────
def test_reanalyze_without_product_is_fast(client, monkeypatch):
    """商品名を含まない訂正では検索ツールを渡さず、写真解析と同じ速さで返すこと。"""
    captured = _capture(monkeypatch)
    r = client.post("/reanalyze", data={
        "correction": "白米→75g、鶏天→山芋天3本、焼き鳥2本→ねぎま塩・皮塩",
    })
    assert r.status_code == 200
    assert "tools" not in captured, "家庭料理の訂正で検索すると再計算が何倍も遅くなる"
    assert captured.get("timeout") == m.REANALYZE_TIMEOUT_SEC
    assert captured.get("time_budget") == m.REANALYZE_TOTAL_BUDGET_SEC
    assert captured.get("max_tokens") == m.ANALYZE_MAX_TOKENS


def test_reanalyze_with_product_keeps_search_but_is_bounded(client, monkeypatch):
    """商品名の訂正では検索を使うが、持ち時間は必ず有限で端末側の上限に収まること。"""
    captured = _capture(monkeypatch)
    r = client.post("/reanalyze", data={"correction": "これはセブンのチョコバナナアイスです"})
    assert r.status_code == 200
    assert captured.get("tools") == [m.WEB_SEARCH_TOOL]
    assert captured.get("timeout") == m.REANALYZE_SEARCH_TIMEOUT_SEC
    assert captured.get("time_budget") == m.REANALYZE_SEARCH_TOTAL_BUDGET_SEC


def test_reanalyze_budgets_fit_in_client_limits():
    """サーバーの持ち時間は、端末側が待つ上限より短いこと。
    逆転すると、サーバーが答えを作っている途中で端末が通信を切り「反映されない」。"""
    html = _read("templates/index.html")
    fetch_ms = int(re.search(r"REANALYZE_FETCH_TIMEOUT_MS\s*=\s*(\d+)", html).group(1))
    total_ms = int(re.search(r"REANALYZE_TOTAL_DEADLINE_MS\s*=\s*(\d+)", html).group(1))
    assert m.REANALYZE_SEARCH_TOTAL_BUDGET_SEC < fetch_ms / 1000
    assert m.REANALYZE_TOTAL_BUDGET_SEC < m.REANALYZE_SEARCH_TOTAL_BUDGET_SEC
    assert fetch_ms < total_ms


# ── ③ 訂正が複数あっても全部反映させる ──────────────────────────
def test_reanalyze_prompt_requires_applying_every_correction(client, monkeypatch):
    """訂正が複数書かれていても、1つだけ直して終わりにさせないこと。"""
    captured = _capture(monkeypatch)
    prev = '{"foods":[{"name":"白米","category":"B","b_count":1}]}'
    r = client.post("/reanalyze", data={
        "correction": "白米→75g、鶏天→山芋天3本",
        "previous": prev,
    })
    assert r.status_code == 200
    sent = "".join(
        c.get("text", "") for c in captured["messages"][0]["content"] if c.get("type") == "text"
    )
    assert "1つ残らず全て反映" in sent, "複数の訂正をすべて反映させる指示がありません"
    assert "訂正のほうを正しいものとして採用" in sent, "写真より訂正を優先させる指示がありません"
    assert "訂正反映：" in sent, "直した項目に印を残す指示がありません"


# ── ④ 他の端末で入れた訂正が反映されること ──────────────────────
def test_sync_updates_existing_item_results():
    """同じIDの項目でも、サーバーのほうが新しければ結果を取り込むこと。
    以前は「この端末に無い項目」しか取り込まず、スマホで訂正してもPC側は古いままだった。"""
    html = _read("templates/index.html")
    start = html.index("async function syncTodayMealsFromServer(")
    body = html[start:start + 4000]
    assert "serverById" in body, "サーバー側の項目をIDで引く処理がありません"
    assert "i.result = sv.result" in body, "既存項目の結果を新しい結果へ更新していません"
    assert "i.loading" in body, "解析中の項目を保護していません（結果を古い内容で塗り潰す）"
