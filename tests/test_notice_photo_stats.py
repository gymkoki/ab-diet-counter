"""お知らせ No.39「減量に成功している人は1日に何枚写真をアップしているか」の回帰テスト。

オーナー指示 2026-10-17：減量成功した人は1日平均で何枚写真をアップしているかを調べて、
最新のお知らせでその結果を全会員に伝えて。

数字は本番の実データから、デイリーレポート④「記録のしかたの違い」と同じ計算で出し、
最初に集計した値で固定する（会員によって数字が変わらないように）。
"""
import datetime
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
import app as m  # noqa: E402


def _html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def _clear(cur):
    for t in ("daily_weight", "action_log", "daily_nutrition", "user_profile"):
        cur.execute(f"DELETE FROM {t}")
    cur.execute(f"DELETE FROM settings WHERE key={m.PH}", (m.NOTICE_PHOTO_STATS_KEY,))


@pytest.fixture
def planted():
    """成功群4人：1日3枚（うち1人は4枚）。失敗群4人：1日1枚（うち1人は2枚）。"""
    m.init_db()
    today = datetime.datetime.now(m.JST).date()
    conn = m._get_conn()
    cur = conn.cursor()
    _clear(cur)

    def member(uid, slope30, photos):
        for i in range(0, 40, 4):
            d = (today - datetime.timedelta(days=i)).isoformat()
            cur.execute(f"INSERT INTO daily_weight (user_id,date,weight,created_at) "
                        f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, d, 65 - slope30 * i / 30, d))
        cur.execute(f"INSERT INTO user_profile (user_id, goal, updated_at) VALUES ({m.PH},'cut','x')", (uid,))
        for i in range(1, 11):
            d = (today - datetime.timedelta(days=i)).isoformat()
            for k in range(photos):
                cur.execute(f"INSERT INTO action_log (user_id, action, created_at) VALUES ({m.PH},'photo',{m.PH})",
                            (uid, f"{d}T{8 + k * 4:02d}:00:00+09:00"))

    for k in range(4):
        member(f"ok-{k}", -1.2, 4 if k == 0 else 3)
    for k in range(4):
        member(f"ng-{k}", +0.6, 2 if k == 0 else 1)
    conn.commit()
    conn.close()
    yield
    conn = m._get_conn()
    cur = conn.cursor()
    _clear(cur)
    conn.commit()
    conn.close()


def test_stats_from_real_data(planted):
    s = m._notice_photo_stats()
    assert s["ok"] is True
    assert s["success_mean"] == 3.2 and s["success_n"] == 4      # (4+3+3+3)/4
    assert s["fail_mean"] == 1.2 and s["fail_n"] == 4             # (2+1+1+1)/4


def test_stats_are_frozen_after_first_computation(planted):
    """最初の集計値で固定し、あとから記録が増えても会員に見せる数字は変わらない。"""
    first = m._notice_photo_stats()
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM action_log")
    conn.commit()
    conn.close()
    assert m._notice_photo_stats() == first


def test_not_enough_members_shows_no_number():
    m.init_db()
    conn = m._get_conn()
    cur = conn.cursor()
    _clear(cur)
    conn.commit()
    conn.close()
    s = m._notice_photo_stats()
    assert s["ok"] is False
    assert not m._get_setting(m.NOTICE_PHOTO_STATS_KEY), "人数が足りない値を固定してはいけない"


def test_public_endpoint_returns_aggregates_only(planted):
    with m.app.test_client() as c:
        d = c.get("/api/notice-stats").get_json()
    assert d["ok"] and d["success_mean"] == 3.2
    assert "members" not in d and "ok-0" not in str(d), "会員を特定できる情報を返さない"


def test_notice_39_is_published_with_synced_key():
    html = _html()
    no = int(re.search(r"最新アップデート No\.(\d+)", html).group(1))
    assert no >= 39
    if no != 39:
        return   # 次のお知らせに入れ替わったら、この検査は役目を終える（番号と鍵の対応は test_update_notice が見る）
    body = html[html.index("最新アップデート No.39"):html.index("（奥松）")]
    assert "1日に何枚写真をアップしている" in body
    assert 'id="notice-photo-text"' in body
    assert "1枚にまとめて" in body, "No.37の「1回の食事は1枚に」と矛盾しないこと"


def test_notice_fills_number_from_server():
    html = _html()
    assert "fillNoticePhotoStats();" in html
    fn = html[html.index("async function fillNoticePhotoStats()"):]
    fn = fn[:fn.index("\n}\n")]
    assert "/api/notice-stats" in fn
    assert "改めてお知らせします" in fn, "数字が無いときの文言"
    assert "Number(d.fail_mean) < ok" in fn, "成功群の方が多いときだけ差を書く"
