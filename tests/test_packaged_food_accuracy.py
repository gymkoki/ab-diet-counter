# 市販パッケージ商品・バナナの判定精度の回帰テスト。
# 経緯（オーナー指摘 2026-08）：
#   ・バナナ1本を撮影したら「90kcal → B0」になった（B食材なのにノーカウント）
#   ・セブンのチョコバナナアイス(240kcal)をパッケージごと撮影したら B2 になった
#     （商品を「アイス」「チョコ」「バナナ」に分解して二重計上していた）
# 対策：①バナナは本数基準で最低B0.5 ②パッケージ商品は商品まるごと1項目
#      ③写真解析ではAIがWeb検索で実測kcalを確認できるようにする

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


# ── ① バナナのルール ────────────────────────────────────────────
def test_prompt_banana_is_at_least_half_count():
    """バナナ1本は必ずB0.5。『120kcal未満だからB0』に丸めさせない。"""
    p = m.ANALYSIS_PROMPT
    assert "バナナ" in p
    assert "1本食べたら必ず【B0.5】" in p, "バナナ1本＝B0.5 の明示ルールがありません"
    assert "kcalのしきい値で下げないこと" in p


def test_prompt_banana_exception_is_declared_in_step2():
    """STEP2のしきい値ルール側にも例外を明記し、矛盾した指示にしないこと。"""
    p = m.ANALYSIS_PROMPT
    assert "このしきい値の例外" in p
    assert "120kcal未満でも下記の最低カウント" in p
    # 表示kcalとBカウントの一致ルールとも整合が取れていること
    assert "ただしバナナだけは例外" in p
    assert "表示kcalを120に水増しせず" in p


def test_prompt_banana_exception_is_narrow():
    """例外を広げず、バナナ以外に波及させないこと。"""
    assert "これ以外の食材で、120kcal未満なのにカウントを付けることはしない" in m.ANALYSIS_PROMPT


# ── ② パッケージ商品は「まるごと1項目」 ──────────────────────────
def test_prompt_packaged_product_counted_as_one_item():
    """商品を中身に分解して二重計上させないルールがあること。"""
    p = m.ANALYSIS_PROMPT
    assert "パッケージ商品は「商品まるごと1つ」で1項目として数える" in p
    assert "分解すると同じカロリーを何度も数えて" in p
    # 実際に起きた誤りを具体例として残す（240kcal → B1 であって B2 ではない）
    assert "チョコバナナアイス" in p
    assert "240kcal は 200〜400 の範囲なので B1" in p


def test_prompt_steps_are_in_order():
    """手順の番号が順番どおりに並んでいること（AIが読み飛ばさないように）。"""
    p = m.ANALYSIS_PROMPT
    pos = [p.index(f"【手順{n}") for n in (1, 2, 3, 4)]
    assert pos == sorted(pos), "手順の番号が前後しています"


# ── ③ Web検索でパッケージ商品の実測kcalを確認する ───────────────
def test_web_search_tool_declared_with_current_type():
    """Web検索ツールが現行のツールタイプで宣言されていること。"""
    assert m.WEB_SEARCH_TOOL["type"] == "web_search_20260209"
    assert m.WEB_SEARCH_TOOL["name"] == "web_search"
    assert m.WEB_SEARCH_TOOL["max_uses"] == 2   # 費用と時間の上限


def test_prompt_limits_web_search_to_packaged_products():
    """検索は市販商品でカロリーが不確かなときだけ。手料理では検索しない。"""
    p = m.ANALYSIS_PROMPT
    assert "web_search ツールで商品名＋「カロリー」を検索" in p
    assert "手料理・家庭料理・一般的な食材（ご飯・肉・野菜など）では検索しない" in p
    assert "調べた実測kcalを reason 欄に必ず残す" in p


class _Blk:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


_OK_JSON = '{"foods": [{"name": "チョコバナナアイス", "kcal_per_serving": 240, "b_count": 1}]}'


@pytest.fixture
def client(monkeypatch):
    m.init_db()
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c


def _png():
    # 1x1 の最小PNG（実際の中身は使わないのでダミーで十分）
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


def test_analyze_declares_web_search_tool(client, monkeypatch):
    """写真解析(/analyze)のリクエストにWeb検索ツールが付いていること。"""
    captured = {}

    def fake_create_and_parse(_client, **kwargs):
        captured.update(kwargs)
        return {"foods": []}

    monkeypatch.setattr(m, "create_and_parse", fake_create_and_parse)
    monkeypatch.setattr(m, "get_client", lambda: object())
    r = client.post("/analyze", data={"image": (__import__("io").BytesIO(_png()), "a.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    assert captured.get("tools") == [m.WEB_SEARCH_TOOL], "/analyze にWeb検索ツールが付いていません"


def test_reanalyze_declares_web_search_tool(client, monkeypatch):
    """訂正での再計算(/reanalyze)でも商品名の実測kcalを調べ直せること。"""
    captured = {}

    def fake_create_and_parse(_client, **kwargs):
        captured.update(kwargs)
        return {"foods": []}

    monkeypatch.setattr(m, "create_and_parse", fake_create_and_parse)
    monkeypatch.setattr(m, "get_client", lambda: object())
    r = client.post("/reanalyze", data={"correction": "これはセブンのチョコバナナアイスです"})
    assert r.status_code == 200
    assert captured.get("tools") == [m.WEB_SEARCH_TOOL], "/reanalyze にWeb検索ツールが付いていません"


def test_analyze_text_has_no_web_search(client, monkeypatch):
    """文章入力は60秒で打ち切る設計のため、あえて検索を付けないこと
    （2026-07の「通信エラー」障害を再発させない）。"""
    captured = {}

    def fake_create_and_parse(_client, **kwargs):
        captured.update(kwargs)
        return {"foods": []}

    monkeypatch.setattr(m, "create_and_parse", fake_create_and_parse)
    monkeypatch.setattr(m, "get_client", lambda: object())
    r = client.post("/analyze-text", data={"text": "バナナ1本"})
    assert r.status_code == 200
    assert "tools" not in captured, "/analyze-text に検索ツールが付くとタイムアウトしやすくなります"
    assert captured.get("timeout") == 60.0


def test_coach_ai_does_not_use_web_search():
    """コーチAIなど解析以外の呼び出しに検索ツールを付けない（費用の無駄）。"""
    src = _read("app.py")
    # tools=[WEB_SEARCH_TOOL] は写真解析まわりの2か所だけ
    assert src.count("tools=[WEB_SEARCH_TOOL]") == 2


# ── サーバー側ツールの中断(pause_turn)から自動で復帰する ────────────
def test_pause_turn_is_resumed_automatically():
    """検索の途中で pause_turn が返っても、続きを取りに行って完成した本文を得ること。"""
    fake = _FakeClient([
        _Resp([_Blk("server_tool_use")], stop_reason="pause_turn"),
        _Resp([_Blk("web_search_tool_result"), _Blk("text", _OK_JSON)]),
    ])
    resp = m._create_with_server_tools(fake, messages=[{"role": "user", "content": "x"}])
    assert resp.stop_reason == "end_turn"
    assert len(fake.messages.calls) == 2
    # 2回目は「これまでのAIの応答」を会話に足して送り直している
    sent = fake.messages.calls[1]["messages"]
    assert sent[-1]["role"] == "assistant"


def test_pause_turn_loop_is_bounded():
    """pause_turn が続いても無限ループしないこと（最大3回で打ち切る）。"""
    fake = _FakeClient([_Resp([_Blk("server_tool_use")], stop_reason="pause_turn") for _ in range(10)])
    m._create_with_server_tools(fake, messages=[{"role": "user", "content": "x"}])
    assert len(fake.messages.calls) == 4   # 初回 + 再開3回


def test_parse_ignores_search_result_blocks():
    """検索結果ブロックが混ざっても、本文JSONだけを正しく取り出せること。"""
    resp = _Resp([_Blk("server_tool_use"), _Blk("web_search_tool_result"), _Blk("text", _OK_JSON)])
    result = m.parse_ai_result(resp)
    assert result["foods"][0]["b_count"] == 1


def test_create_and_parse_resumes_pause_turn_end_to_end():
    """create_and_parse 経由でも pause_turn からの復帰が効くこと。"""
    fake = _FakeClient([
        _Resp([_Blk("server_tool_use")], stop_reason="pause_turn"),
        _Resp([_Blk("web_search_tool_result"), _Blk("text", _OK_JSON)]),
    ])
    result = m.create_and_parse(fake, messages=[{"role": "user", "content": "x"}])
    assert result["foods"][0]["name"] == "チョコバナナアイス"
