# iPhone「ショートカット」連携（撮影 → 食事判定 → 確認 → アップ）の回帰テスト。
# オーナー指示 2026-08：写真を撮ったら「ABダイエットにアップしますか？」と出して、
# 「はい」でそのまま記録できるようにする。

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402


def _jpeg_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 170, 120)).save(buf, "JPEG")
    return buf.getvalue()


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


@pytest.fixture
def client(monkeypatch):
    m.init_db()
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c


ANALYSIS = {
    "foods": [{"name": "サンドイッチ", "category": "B", "kcal_per_serving": 320, "b_count": 1,
               "protein_g": 15, "veg_g": 30, "fruit_g": 0, "reason": "パン＋具"}],
    "total_b_count": 1, "total_protein_g": 15, "total_veg_g": 30, "total_fruit_g": 0,
    "advice": "野菜をもう少し足しましょう。",
}


def _upload(client, uid="sc-user-1"):
    return client.post("/api/shortcut/upload", data={
        "user_id": uid,
        "image": (io.BytesIO(_jpeg_bytes()), "photo.jpg"),
    }, content_type="multipart/form-data")


def test_check_returns_is_food_true(client, monkeypatch):
    class _C:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp('{"is_food": true}')
    monkeypatch.setattr(m, "get_client", lambda: _C())
    r = client.post("/api/shortcut/check", data={
        "image": (io.BytesIO(_jpeg_bytes()), "photo.jpg")},
        content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["is_food"] is True


def test_check_returns_false_for_non_food(client, monkeypatch):
    class _C:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp('{"is_food": false}')
    monkeypatch.setattr(m, "get_client", lambda: _C())
    r = client.post("/api/shortcut/check", data={
        "image": (io.BytesIO(_jpeg_bytes()), "photo.jpg")},
        content_type="multipart/form-data")
    assert r.get_json()["is_food"] is False


def test_check_failure_does_not_prompt(client, monkeypatch):
    """判定に失敗したら is_food=false（毎回確認が出るのを防ぐ）。"""
    class _C:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("boom")
    monkeypatch.setattr(m, "get_client", lambda: _C())
    r = client.post("/api/shortcut/check", data={
        "image": (io.BytesIO(_jpeg_bytes()), "photo.jpg")},
        content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["is_food"] is False


def test_upload_saves_meal_to_today(client, monkeypatch):
    """アップすると今日の食事記録に追加され、通知用メッセージが返る。"""
    monkeypatch.setattr(m, "get_client", lambda: object())
    monkeypatch.setattr(m, "create_and_parse", lambda c, **kw: dict(ANALYSIS))
    uid = "sc-user-save"
    r = _upload(client, uid)
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["ok"] is True and d["total_b_count"] == 1
    assert "B1" in d["message"] and "🥩15g" in d["message"]

    # 今日の payload に保存されていること（アプリ側はこれを取り込む）
    today = m.datetime.datetime.now(m.JST).strftime("%Y-%m-%d")
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute(f"SELECT payload FROM daily_meals WHERE user_id={m.PH} AND date={m.PH}", (uid, today))
        row = cur.fetchone(); conn.close()
    items = json.loads(row[0])["food"]["items"]
    assert len(items) == 1
    assert items[0]["result"]["total_b_count"] == 1
    assert items[0]["fromShortcut"] is True
    assert items[0]["previewSrc"].startswith("data:image/jpeg;base64,")   # アプリに写真が出る
    assert items[0]["id"] > 10 ** 14   # アプリと同じミリ秒×100のID方式


def test_upload_appends_without_losing_existing(client, monkeypatch):
    """既にある今日の記録を消さずに追記する。"""
    monkeypatch.setattr(m, "get_client", lambda: object())
    monkeypatch.setattr(m, "create_and_parse", lambda c, **kw: dict(ANALYSIS))
    uid = "sc-user-append"
    _upload(client, uid)
    _upload(client, uid)
    today = m.datetime.datetime.now(m.JST).strftime("%Y-%m-%d")
    with m._db_lock:
        conn = m._get_conn(); cur = conn.cursor()
        cur.execute(f"SELECT payload FROM daily_meals WHERE user_id={m.PH} AND date={m.PH}", (uid, today))
        row = cur.fetchone(); conn.close()
    items = json.loads(row[0])["food"]["items"]
    assert len(items) == 2
    assert items[0]["id"] != items[1]["id"]


def test_upload_requires_user_id(client, monkeypatch):
    monkeypatch.setattr(m, "get_client", lambda: object())
    r = client.post("/api/shortcut/upload", data={
        "image": (io.BytesIO(_jpeg_bytes()), "photo.jpg")},
        content_type="multipart/form-data")
    assert r.status_code == 400
    assert "連携コード" in r.get_json()["message"]


def test_settings_screen_has_shortcut_guide():
    """設定画面にショートカットの案内とコピー機能があること。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "index.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    assert "撮った写真をそのまま記録" in html
    assert "function copyShortcutId" in html
    assert "/api/shortcut/check" in html and "/api/shortcut/upload" in html
