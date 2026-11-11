# 食事アルバム（履歴から「その日に何を食べたか」を見返す）の回帰テスト（会員の声 2026-10-14）。
#
# 「履歴のグラフを見て、体重管理が上手くいっている時に何を食べていたのか確認したい」。
# 明細(daily_meals)は5日で消えるため、記録1件ごとに小さなサムネ・料理名・Bを別に残す。
#
# 守りたい不変条件：
#   ①食事を保存するとアルバムに入る（サムネは小さく縮小・料理名とBも残る）
#   ②会員が記録を消したら、アルバムからも消える
#   ③明細が5日で消えても、アルバムは残る。MEAL_ALBUM_RETAIN_DAYS より古い分は消える
#   ④自分の分しか返らない
#   ⑤アルバムの処理が失敗しても、食事の保存そのものは成功する
#   ⑥アプリの履歴：一覧にサムネ、グラフの日付タップ・一覧タップでその日の食事を開ける

import base64
import datetime
import io
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app as m  # noqa: E402

PIL = pytest.importorskip("PIL.Image", reason="Pillow 未インストール")
PREFIX = "album-"


def _exec(sql, params=()):
    with m._db_lock:
        conn = m._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


@pytest.fixture
def db():
    m.init_db()

    def clean():
        for t in ("meal_album", "daily_meals", "daily_nutrition", "user_profile"):
            _exec(f"DELETE FROM {t} WHERE user_id LIKE {m.PH}", (PREFIX + "%",))
    clean()
    yield
    clean()


def _photo(px=600):
    im = PIL.new("RGB", (px, px), (200, 120, 60))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _item(iid, names, b, photo=True, text=None):
    it = {"id": iid, "result": {"foods": [{"name": n, "category": "B", "b_count": 1} for n in names],
                                "total_b_count": b}}
    if photo:
        it["previewSrc"] = _photo()
    if text:
        it["text"] = text
    return it


def _save(uid, date, items):
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        r = c.post("/api/daily-meals", json={"user_id": uid, "date": date,
                                              "payload": {"food": {"items": items}}})
    assert r.status_code == 200


def _album(uid):
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        r = c.get(f"/api/meal-album?user_id={uid}")
    assert r.status_code == 200
    return r.get_json()["days"]


def _day(ago):
    return (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=ago)).isoformat()


def test_saved_meals_appear_in_album(db):
    uid, d = PREFIX + "a", _day(1)
    _save(uid, d, [_item(178000000000001, ["白米", "焼き鮭"], 2),
                   _item(178000000000002, ["サラダ"], 0, photo=False, text="サラダ")])
    items = _album(uid)[d]
    assert [i["foods"] for i in items] == ["白米・焼き鮭", "サラダ"]
    assert items[0]["b_count"] == 2
    assert items[0]["thumb"].startswith("data:image/jpeg;base64,")
    assert items[1]["thumb"] is None          # 文章だけの記録はサムネなし
    # サムネは小さく縮小されている（DBを膨らませない）
    raw = base64.b64decode(items[0]["thumb"].split(",", 1)[1])
    w, h = PIL.open(io.BytesIO(raw)).size
    assert max(w, h) <= m.MEAL_ALBUM_THUMB_PX
    assert len(raw) < 30_000


def test_deleted_record_leaves_album(db):
    uid, d = PREFIX + "b", _day(1)
    _save(uid, d, [_item(1, ["カレー"], 3), _item(2, ["ケーキ"], 1)])
    _save(uid, d, [_item(1, ["カレー"], 3)])
    assert [i["foods"] for i in _album(uid)[d]] == ["カレー"]


def test_reanalysis_updates_names_but_keeps_thumb(db):
    uid, d = PREFIX + "c", _day(1)
    _save(uid, d, [_item(1, ["うどん"], 2)])
    first = _album(uid)[d][0]["thumb"]
    item = _item(1, ["うどん（2玉）"], 4, photo=False)    # 訂正後の保存（写真を付けずに送られても）
    _save(uid, d, [item])
    got = _album(uid)[d][0]
    assert got["foods"] == "うどん（2玉）" and got["b_count"] == 4
    assert got["thumb"] == first, "訂正のたびにサムネが消えてしまう"


def test_album_outlives_meal_details_but_not_retention(db):
    uid = PREFIX + "d"
    old_ok = _day(20)                                 # 明細は5日で消えるが、アルバムには残る
    too_old = _day(m.MEAL_ALBUM_RETAIN_DAYS + 3)      # 保存期間を過ぎた分は消える
    _save(uid, too_old, [_item(1, ["ラーメン"], 3)])
    _save(uid, old_ok, [_item(2, ["そば"], 2)])
    _save(uid, _day(0), [_item(3, ["おにぎり"], 1)])  # 今日の保存で古い明細が掃除される
    with m._db_lock:
        conn = m._get_conn()
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM daily_meals WHERE user_id={m.PH} AND date={m.PH}", (uid, old_ok))
        assert cur.fetchone()[0] == 0, "前提：明細は5日で消える"
        conn.close()
    days = _album(uid)
    assert old_ok in days and _day(0) in days
    assert too_old not in days


def test_backfills_from_recent_details(db):
    """アルバム導入前に保存された直近の明細も、初めて開いたときに取り込む。"""
    uid, d = PREFIX + "e", _day(2)
    payload = json.dumps({"food": {"items": [_item(5, ["親子丼"], 3)]}})
    _exec(f"INSERT INTO daily_meals (user_id, date, payload, created_at) VALUES ({m.PH},{m.PH},{m.PH},'x')",
          (uid, d, payload))
    assert [i["foods"] for i in _album(uid)[d]] == ["親子丼"]


def test_only_own_album(db):
    _save(PREFIX + "me", _day(1), [_item(1, ["牛丼"], 3)])
    assert _album(PREFIX + "someone-else") == {}


def test_album_failure_does_not_break_saving(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "_sync_meal_album", boom)
    _save(PREFIX + "f", _day(0), [_item(1, ["パン"], 1)])     # 200 が返ること（_save の中で確認）


# ── アプリ側 ──────────────────────────────────────────────
def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def test_history_shows_thumbs_and_opens_album():
    html = _html()
    assert 'id="meal-album-overlay"' in html
    assert "function openMealAlbum(date)" in html and "function _paintHistoryThumbs()" in html
    assert "data-hist-date=" in html
    # 一覧を描き直したらサムネも差し込み直す
    paint = re.search(r"function _paintHistory\(daily\) \{(.*?)\n\}", html, re.S).group(1)
    assert "_paintHistoryThumbs()" in paint
    # 履歴を開いたらアルバムを読み込む
    load = re.search(r"async function loadHistory\(\) \{(.*?)\n\}", html, re.S).group(1)
    assert "loadMealAlbum()" in load


def test_chart_dates_open_album():
    html = _html()
    chart = re.search(r"function renderWeightChart\(daily\) \{(.*?)\n\}", html, re.S).group(1)
    assert "openMealAlbum(" in chart, "グラフの日付をタップしてもその日の食事が開かない"


def test_lightbox_sits_above_album():
    html = _html()
    lb = int(re.search(r"#lightbox \{[^}]*z-index:(\d+)", html).group(1))
    assert lb > 500, "アルバムの写真を拡大しても、モーダルの裏に隠れてしまう"


# ── ⑦ どの食材がBだったかを遡って見られる（会員の声 2026-11-11） ─────────────────
def _mixed_item(iid):
    return {"id": iid, "result": {"foods": [
        {"name": "白米", "category": "B", "b_count": 1},
        {"name": "鶏むね肉のソテー", "category": "A", "b_count": 0},
        {"name": "調理油", "category": "B", "b_count": 0.5},
        {"name": "サラダ", "category": "A", "b_count": 0},
    ], "total_b_count": 1.5}}


def test_album_keeps_food_breakdown(db):
    uid = PREFIX + "detail"
    d = datetime.datetime.now(m.JST).date().isoformat()
    _save(uid, d, [_mixed_item(1700000000001)])
    it = _album(uid)[d][0]
    assert it["items"] == [{"n": "白米", "b": 1.0, "c": "B"}, {"n": "鶏むね肉のソテー", "b": 0.0, "c": "A"},
                           {"n": "調理油", "b": 0.5, "c": "B"}, {"n": "サラダ", "b": 0.0, "c": "A"}]
    assert it["b_count"] == 1.5


def test_breakdown_survives_after_details_are_deleted(db):
    """明細(daily_meals)が5日で消えたあとも、内訳はアルバムに残る。"""
    uid = PREFIX + "keep"
    d = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=20)).isoformat()
    _save(uid, d, [_mixed_item(1700000000002)])
    _exec(f"DELETE FROM daily_meals WHERE user_id={m.PH}", (uid,))
    it = _album(uid)[d][0]
    assert [f["n"] for f in it["items"] if f["b"]] == ["白米", "調理油"]


def test_rows_from_before_breakdown_are_filled_from_recent_details(db):
    """内訳を残し始める前にアルバムへ入った直近の日も、明細が残っていれば内訳を補う。"""
    uid = PREFIX + "old"
    d = datetime.datetime.now(m.JST).date().isoformat()
    _save(uid, d, [_mixed_item(1700000000003)])
    _exec(f"UPDATE meal_album SET detail=NULL WHERE user_id={m.PH}", (uid,))
    it = _album(uid)[d][0]
    assert it["items"] and it["items"][0]["n"] == "白米"


def test_old_rows_without_breakdown_still_show(db):
    uid = PREFIX + "legacy"
    d = (datetime.datetime.now(m.JST).date() - datetime.timedelta(days=30)).isoformat()
    _exec(f"INSERT INTO meal_album (user_id,date,item_id,slot,foods,b_count,created_at) "
          f"VALUES ({m.PH},{m.PH},'1','昼','カレーライス',2,'x')", (uid, d))
    it = _album(uid)[d][0]
    assert it["items"] is None and it["foods"] == "カレーライス"


def test_food_breakdown_is_limited():
    res = {"foods": [{"name": "x" * 100, "b_count": True, "category": "b"}] * 50}
    out = json.loads(m._album_food_detail(res))
    assert len(out) == m.MEAL_ALBUM_DETAIL_MAX and len(out[0]["n"]) == 40
    assert out[0]["b"] is None and out[0]["c"] == "B"
    assert m._album_food_detail({"foods": []}) is None


def test_album_popup_lists_foods_with_b():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert "${albumFoodsHtml(it.items)}" in html
    fn = re.search(r"function albumFoodsHtml\(items\) \{.*?\n\}", html, re.S).group(0)
    assert "is-b" in fn and "esc(f.n" in fn, "食材名はエスケープし、Bの食材を目立たせる"
