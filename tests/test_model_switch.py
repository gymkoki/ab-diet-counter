# 解析モデルの比較・切り替え（Sonnet 5 ⇔ Sonnet 5.5）の回帰テスト（オーナー指示 2026-11-28：
# 「Sonnet 5.5 が同じ以上の精度なら、そのまま乗り換えて」）。
#   ①Sonnet 5.5 は thinking={"type":"disabled"} を受け付けない（400）→ between_tools に置き換える
#   ②5.5 が断った（refusal）ときは Sonnet 5 で1回だけ作り直す（比較テストでは作り直さない）
#   ③使うモデルは管理画面で切り替え（settings。未設定・不正な値は環境変数＝従来どおり）
#   ④比較テスト：直近の会員の写真を両方のモデルで解析し、管理画面に並べる・判定を残す
# AIは呼ばない（偽のクライアント）。
import base64
import datetime
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402
from conftest import seed_weight  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
RESULT = {"foods": [{"name": "白米", "category": "B", "kcal_per_serving": 250, "amount": "1杯",
                     "b_count": 1, "protein_g": 4, "veg_g": 0, "reason": "主食"}],
          "total_b_count": 1, "total_protein_g": 4, "total_veg_g": 0, "advice": "よい"}


def _resp(text, stop="end_turn"):
    blocks = [SimpleNamespace(type="text", text=text)] if text else []
    return SimpleNamespace(content=blocks, stop_reason=stop)


class FakeClient:
    """messages.create の呼び出しを記録し、用意した応答を順に返す。"""
    def __init__(self, responses=None, by_model=None):
        self.calls = []
        self.responses = list(responses or [])
        self.by_model = by_model or {}
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        if kw.get("model") in self.by_model:
            return self.by_model[kw["model"]]()
        return self.responses.pop(0)


def _reset_model():
    m._analysis_model_cache.update(v=None, at=0.0)
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        for k in (m.ANALYSIS_MODEL_SETTING_KEY, m.MODEL_COMPARE_KEY, m.MODEL_COMPARE_GRADES_KEY):
            cur.execute(f"DELETE FROM settings WHERE key={m.PH}", (k,))
        cur.execute("DELETE FROM daily_meals WHERE user_id LIKE 'mc-%'")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def clean():
    m.init_db()
    _reset_model()
    yield
    _reset_model()


# ── ① 思考オフの指定 ───────────────────────────────────────────
def test_thinking_off_per_model():
    assert m._thinking_off("claude-sonnet-5-5") == {"type": "between_tools"}
    assert m._thinking_off("claude-sonnet-5") == {"type": "disabled"}


def test_create_and_parse_sends_between_tools_for_sonnet_55():
    c = FakeClient([_resp(json.dumps(RESULT, ensure_ascii=False))])
    res = m.create_and_parse(c, model="claude-sonnet-5-5", thinking=m.THINKING_OFF, max_tokens=100,
                             messages=[{"role": "user", "content": "x"}])
    assert res["total_b_count"] == 1
    assert c.calls[0]["thinking"] == {"type": "between_tools"}
    assert m.THINKING_OFF == {"type": "disabled"}, "共有の定数を書き換えていない"


# ── ② 断られたら Sonnet 5 で作り直す ─────────────────────────────────
def test_refusal_falls_back_to_sonnet_5_once():
    c = FakeClient([_resp("", stop="refusal"), _resp(json.dumps(RESULT, ensure_ascii=False))])
    res = m.create_and_parse(c, model="claude-sonnet-5-5", thinking=m.THINKING_OFF, max_tokens=100,
                             messages=[{"role": "user", "content": "x"}])
    assert res["total_b_count"] == 1
    assert [x["model"] for x in c.calls] == ["claude-sonnet-5-5", "claude-sonnet-5"]
    assert c.calls[1]["thinking"] == {"type": "disabled"}


def test_compare_does_not_fall_back():
    c = FakeClient(by_model={"claude-sonnet-5-5": lambda: _resp("", stop="refusal")})
    with pytest.raises(m.EmptyAIResponse):
        m.create_and_parse(c, model="claude-sonnet-5-5", thinking=m.THINKING_OFF, max_tokens=100,
                           allow_fallback=False, time_budget=0.5,
                           messages=[{"role": "user", "content": "x"}])
    assert {x["model"] for x in c.calls} == {"claude-sonnet-5-5"}


# ── ③ 管理画面で切り替え ─────────────────────────────────────────
def test_switch_model_from_admin():
    assert m._analysis_model() == m.ANALYSIS_MODEL
    with m.app.test_client() as c:
        assert c.post("/api/admin/analysis-model", json={"model": "claude-sonnet-5-5"}).status_code in (401, 403, 404)
        assert c.post("/api/admin/analysis-model", json={"model": "gpt-x"}, headers=ADMIN).status_code == 400
        r = c.post("/api/admin/analysis-model", json={"model": "claude-sonnet-5-5"}, headers=ADMIN)
        assert r.status_code == 200 and r.get_json()["current"] == "claude-sonnet-5-5"
    assert m._analysis_model() == "claude-sonnet-5-5"


def test_bad_setting_falls_back_to_env_default():
    m._set_setting(m.ANALYSIS_MODEL_SETTING_KEY, "claude-unknown")
    m._analysis_model_cache.update(v=None, at=0.0)
    assert m._analysis_model() == m.ANALYSIS_MODEL


def test_analyze_text_uses_selected_model(monkeypatch):
    seed_weight("mc-text")
    m._set_setting(m.ANALYSIS_MODEL_SETTING_KEY, "claude-sonnet-5-5")
    m._analysis_model_cache.update(v=None, at=0.0)
    fake = FakeClient([_resp(json.dumps(RESULT, ensure_ascii=False))])
    monkeypatch.setattr(m, "get_client", lambda: fake)
    with m.app.test_client() as c:
        r = c.post("/analyze-text", data={"text": "ごはん1杯", "user_id": "mc-text"})
    assert r.status_code == 200
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"
    assert fake.calls[0]["thinking"] == {"type": "between_tools"}


def test_all_analysis_routes_use_selected_model():
    src = open(m.__file__, encoding="utf-8").read()
    assert "model=ANALYSIS_MODEL," not in src, "解析の入口はすべて _analysis_model() を使う"
    assert src.count("model=_analysis_model()") >= 3


# ── ④ 比較テスト ───────────────────────────────────────────────
def _png_data_url():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 120, 60)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _seed_meal(uid, b=1.5, corrected=False):
    today = datetime.datetime.now(m.JST).date().isoformat()
    reason = "訂正反映：大盛り" if corrected else "主食"
    payload = {"food": {"items": [
        {"id": 177000000000000, "previewSrc": _png_data_url(),
         "result": {"foods": [{"name": "白米", "b_count": b, "reason": reason}], "total_b_count": b}},
        {"id": 177000000000001, "isText": True, "text": "おにぎり", "result": {"foods": [], "total_b_count": 1}},
    ]}}
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO daily_meals (user_id,date,payload,created_at) VALUES ({m.PH},{m.PH},{m.PH},'x')",
                    (uid, today, json.dumps(payload)))
        conn.commit()
    finally:
        conn.close()


def test_compare_runs_both_models_and_shows_results(monkeypatch):
    _seed_meal("mc-a", b=2, corrected=True)
    out = {"claude-sonnet-5": 1, "claude-sonnet-5-5": 2}
    fake = FakeClient(by_model={k: (lambda v=v: _resp(json.dumps(dict(RESULT, total_b_count=v,
                                                                       foods=[dict(RESULT["foods"][0], b_count=v)]),
                                                                  ensure_ascii=False)))
                                for k, v in out.items()})
    monkeypatch.setattr(m, "get_client", lambda: fake)
    m._model_compare_running["on"] = True
    m._run_model_compare()
    assert m._model_compare_running["on"] is False
    with m.app.test_client() as c:
        d = c.get("/api/admin/model-compare", headers=ADMIN).get_json()
        assert d["compare"]["status"] == "done"
        items = d["compare"]["items"]
        assert len(items) == 1, "文章の記録は比べない（写真だけ）"
        it = items[0]
        assert it["a"]["b"] == 1 and it["b"]["b"] == 2 and it["corrected"] is True
        assert it["src"].startswith("data:image") and "uid" not in it, "会員IDは画面に出さない"
        # 会員が訂正した記録（B2）との差：5 は1、5.5 は0
        assert d["summary"]["a"]["corrected_mae"] == 1 and d["summary"]["b"]["corrected_mae"] == 0
        # 判定を残す
        assert c.post("/api/admin/model-compare/grade", json={"i": 0, "pick": "b"}, headers=ADMIN).status_code == 200
        d = c.get("/api/admin/model-compare", headers=ADMIN).get_json()
        assert d["grades"] == {"0": "b"} and d["summary"]["b_better"] == 1
        assert c.post("/api/admin/model-compare/grade", json={"i": 0, "pick": "x"}, headers=ADMIN).status_code == 400
    for call in fake.calls:
        assert call["thinking"] == m._thinking_off(call["model"])


def test_compare_endpoints_are_admin_only():
    with m.app.test_client() as c:
        assert c.get("/api/admin/model-compare").status_code in (401, 403, 404)
        assert c.post("/api/admin/model-compare/start").status_code in (401, 403, 404)
        assert c.post("/api/admin/model-compare/grade", json={"i": 0, "pick": "a"}).status_code in (401, 403, 404)


def test_compare_without_api_key_records_error(monkeypatch):
    monkeypatch.setattr(m, "get_client", lambda: None)
    m._run_model_compare()
    d = m._model_compare_load()
    assert d["status"] == "error" and "APIキー" in d["error"]


def test_admin_card_exists():
    src = open(os.path.join(os.path.dirname(m.__file__), "templates", "admin.html"), encoding="utf-8").read()
    assert 'id="model-compare-card"' in src and "loadModelCompare()" in src
