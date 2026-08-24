# 「間食をまとめて記録」(/analyze-batch) の回帰テスト。
#
# 目的：複数の間食を「1回のAI呼び出し」でまとめて解析する経路が壊れないこと。
#   大きな判定ルール（ANALYSIS_PROMPT）を品数ぶん繰り返し送らずに済むので、
#   1品ずつ /analyze-text を呼ぶより費用が下がる、という機能の芯を守る。
#
# 守りたい不変条件：
#   ①送信は品数によらず1回（利用ログも1回）＝待たせる経路にAI往復を増やさない
#   ②応答が {"results":[...]} でも 素の配列でも コードフェンス付きでも読める
#   ③total_* は各品ごとに内訳(foods)から再計算して整合させる
#   ④結果は入力順に並ぶ（フロントが順番で各品へ割り当てるため）

import json
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
def client():
    m.init_db()
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c


class _Resp:
    """messages.create の戻り値（content ブロック）を模す。"""
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason


# ── ② 応答パーサ parse_batch_results ────────────────────────────
def _one(name, b, p=0, v=0):
    return {"foods": [{"name": name, "category": "B", "b_count": b,
                       "protein_g": p, "veg_g": v}]}


def test_parse_object_form():
    payload = {"results": [_one("チョコ", 1), _one("カフェラテ", 0.5)]}
    out = m.parse_batch_results(_Resp(json.dumps(payload, ensure_ascii=False)))
    assert len(out) == 2
    assert out[0]["foods"][0]["name"] == "チョコ"
    # total_* が内訳から再計算される
    assert out[0]["total_b_count"] == 1
    assert out[1]["total_b_count"] == 0.5


def test_parse_bare_array_form():
    """素の配列（{results:...}で包まない）で返ってきても読める。"""
    arr = [_one("せんべい", 1), _one("お茶", 0)]
    out = m.parse_batch_results(_Resp(json.dumps(arr, ensure_ascii=False)))
    assert [r["foods"][0]["name"] for r in out] == ["せんべい", "お茶"]


def test_parse_code_fence_and_preamble():
    payload = {"results": [_one("プリン", 1.5, p=3)]}
    text = "はい、解析します。\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    out = m.parse_batch_results(_Resp(text))
    assert len(out) == 1
    assert out[0]["total_protein_g"] == 3


def test_parse_single_dict_fallback():
    """1品だけを {"foods":[...]} の素のdictで返した場合も1件として扱う。"""
    out = m.parse_batch_results(_Resp(json.dumps(_one("ガム", 0), ensure_ascii=False)))
    assert len(out) == 1
    assert out[0]["foods"][0]["name"] == "ガム"


def test_parse_empty_raises():
    with pytest.raises(m.EmptyAIResponse):
        m.parse_batch_results(_Resp(""))
    with pytest.raises(m.EmptyAIResponse):
        m.parse_batch_results(_Resp('{"results": []}'))


def test_parse_recomputes_totals_from_foods():
    """AIが返す合計が内訳と食い違っても、内訳から必ず作り直す。"""
    bad = {"results": [{"foods": [{"name": "x", "b_count": 1}, {"name": "y", "b_count": 0.5}],
                        "total_b_count": 9}]}
    out = m.parse_batch_results(_Resp(json.dumps(bad)))
    assert out[0]["total_b_count"] == 1.5


# ── ③ エンドポイント /analyze-batch ─────────────────────────────
def _mock_ai(monkeypatch, results=None):
    captured = {}

    def fake_batch(_client, **kwargs):
        captured.update(kwargs)
        return results if results is not None else [_one("チョコ", 1)]

    monkeypatch.setattr(m, "create_and_parse_batch", fake_batch)
    monkeypatch.setattr(m, "get_client", lambda: object())
    return captured


def test_endpoint_returns_results(client, monkeypatch):
    _mock_ai(monkeypatch, [_one("チョコ", 1), _one("カフェラテ", 0.5)])
    r = client.post("/analyze-batch", data={
        "items": json.dumps(["チョコ2個", "カフェラテ"]),
        "category": "午後の間食", "user_id": "u1",
    })
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["results"]) == 2
    assert body["truncated"] is False


def test_endpoint_empty_items_400(client, monkeypatch):
    _mock_ai(monkeypatch)
    for payload in ({}, {"items": "[]"}, {"items": json.dumps(["  ", ""])}):
        r = client.post("/analyze-batch", data=payload)
        assert r.status_code == 400


def test_endpoint_passes_category_and_items_into_prompt(client, monkeypatch):
    cap = _mock_ai(monkeypatch)
    client.post("/analyze-batch", data={
        "items": json.dumps(["チョコ", "コーヒー"]),
        "category": "夜の間食", "user_id": "u1",
    })
    prompt = cap["messages"][0]["content"][0]["text"]
    assert "夜の間食" in prompt
    assert "1. チョコ" in prompt and "2. コーヒー" in prompt
    # システムプロンプト(判定ルール)は1回だけ・キャッシュ付きで渡す
    assert cap["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_endpoint_truncates_over_max(client, monkeypatch):
    cap = _mock_ai(monkeypatch, [_one(f"s{i}", 1) for i in range(m.SNACK_BATCH_MAX_ITEMS)])
    many = [f"間食{i}" for i in range(m.SNACK_BATCH_MAX_ITEMS + 3)]
    r = client.post("/analyze-batch", data={
        "items": json.dumps(many), "category": "午前の間食", "user_id": "u1",
    })
    body = r.get_json()
    assert body["truncated"] is True
    # プロンプトに載る品数は上限まで
    prompt = cap["messages"][0]["content"][0]["text"]
    assert f"{m.SNACK_BATCH_MAX_ITEMS}. 間食{m.SNACK_BATCH_MAX_ITEMS - 1}" in prompt
    assert f"{m.SNACK_BATCH_MAX_ITEMS + 1}." not in prompt


def test_endpoint_logs_usage_once_per_batch(client, monkeypatch):
    """まとめ解析はAPI呼び出し1回ぶん＝利用ログも1回だけ（費用削減の芯）。"""
    _mock_ai(monkeypatch, [_one("a", 1), _one("b", 1), _one("c", 1)])
    calls = {"n": 0}
    monkeypatch.setattr(m, "_log_usage", lambda uid: calls.__setitem__("n", calls["n"] + 1))
    client.post("/analyze-batch", data={
        "items": json.dumps(["a", "b", "c"]), "category": "午後の間食", "user_id": "u1",
    })
    assert calls["n"] == 1


def test_endpoint_default_category(client, monkeypatch):
    cap = _mock_ai(monkeypatch)
    client.post("/analyze-batch", data={"items": json.dumps(["チョコ"]), "user_id": "u1"})
    assert "間食" in cap["messages"][0]["content"][0]["text"]


# ── ④ フロント側（記録画面の入口とモーダル）──────────────────────
def test_frontend_entry_and_modal_exist():
    html = _read("templates/index.html")
    # 記録グリッドに「間食をまとめて記録」の入口がある
    assert "openSnackBatch(" in html
    assert "間食をまとめて記録" in html
    # モーダルと3区分ボタン
    assert 'id="snack-batch-overlay"' in html
    for cat in ("午前の間食", "午後の間食", "夜の間食"):
        assert cat in html
    # 送信・区分選択の関数が揃っている
    for fn in ("function submitSnackBatch", "function selectSnackCategory", "function closeSnackBatch"):
        assert fn in html
    # 1回でまとめて送る（/analyze-batch を叩く）
    assert "/analyze-batch" in html


def test_frontend_keeps_four_equal_cards():
    """まとめ記録ボタンは幅広の別ボタン。等サイズの4カードは崩さない。"""
    html = _read("templates/index.html")
    panel = re.search(r"function buildPanel\(m\) \{.*?\n\}", html, re.S).group(0)
    assert panel.count('class="rec-card"') == 4
    assert "rec-card-wide" in panel


def test_frontend_batch_items_persist():
    """isSnack / snackCategory が保存対象に含まれる（同期・再読み込みで消えない）。"""
    html = _read("templates/index.html")
    payload = re.search(r"function buildMealsPayload\(includePending\) \{.*?\n\}", html, re.S).group(0)
    assert "isSnack" in payload and "snackCategory" in payload
