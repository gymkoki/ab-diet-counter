"""減量希望で体重が減っておらず、食事の写真が少ない人への「写真のお願い」（オーナー指示 2026-11-08）。

「痩せていない原因が主に、食事写真のアップ枚数の少なさが考えられる時は個別メッセージを送って。
 減量希望者にだけ。強いて言えば…の形で、相手の気を悪くしないように」
"""
import datetime

import pytest

import app as m


def _clean():
    m.init_db()
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("user_profile", "daily_weight", "action_log", "coach_messages", "daily_b_count"):
        cur.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()


@pytest.fixture
def db():
    _clean()
    yield
    _clean()


def _member(uid, goal="cut", w_first=53.1, w_last=54.0, record_days=6, photos_per_day=1, texts_per_day=3,
            name="テスト"):
    """直近30日の体重（最初と最新）と、直近の記録（1日あたり写真◯回・文章◯回）を入れる。"""
    today = datetime.datetime.now(m.JST).date()
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO user_profile (user_id, display_name, goal, updated_at) VALUES ({m.PH},{m.PH},{m.PH},'x')",
                (uid, name, goal))
    for d, w in ((today - datetime.timedelta(days=25), w_first), (today - datetime.timedelta(days=1), w_last)):
        cur.execute(f"INSERT INTO daily_weight (user_id,date,weight,created_at) VALUES ({m.PH},{m.PH},{m.PH},'x')",
                    (uid, d.isoformat(), w))
    for i in range(1, record_days + 1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        acts = ["photo"] * photos_per_day + ["text"] * texts_per_day
        for k, a in enumerate(acts):
            cur.execute(f"INSERT INTO action_log (user_id,action,created_at) VALUES ({m.PH},{m.PH},{m.PH})",
                        (uid, a, f"{d}T{8 + 3 * k:02d}:00:00+09:00"))
    conn.commit()
    conn.close()


def _run(uid):
    """アプリを開いたときと同じ経路（/api/coach-messages）で判定させ、届いたメッセージを返す。"""
    r = m.app.test_client().get(f"/api/coach-messages?user_id={uid}")
    return [i["message"] for i in r.get_json()["items"]]


def _nudges(uid):
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT message FROM coach_messages WHERE user_id={m.PH} AND reason={m.PH}",
                (uid, m.PHOTO_NUDGE_REASON))
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def test_sent_when_not_losing_and_few_photos(db):
    """スクリーンショットの例：30日で+0.9kg・写真は1日約1枚 → 送る。"""
    _member("u1")
    msgs = _run("u1")
    assert len(_nudges("u1")) == 1
    assert any("強いて言えば" in t for t in msgs)
    assert "1日あたり約1枚" in _nudges("u1")[0]


def test_only_once_in_cooldown(db):
    _member("u1")
    _run("u1"); _run("u1"); _run("u1")
    assert len(_nudges("u1")) == 1, "同じ人に何度も送らない（14日に1回まで）"


@pytest.mark.parametrize("kw,why", [
    ({"goal": "maintain"}, "減量希望者だけ"),
    ({"goal": "bulk"}, "減量希望者だけ"),
    ({"w_first": 55.0, "w_last": 54.0}, "体重が減っている人には送らない"),
    ({"photos_per_day": 2}, "写真が1日2枚以上あれば送らない"),
    ({"record_days": 2}, "記録した日が少なすぎると判断できない"),
])
def test_not_sent(db, kw, why):
    _member("u2", **kw)
    _run("u2")
    assert _nudges("u2") == [], why


def test_no_weight_no_message(db):
    _member("u3")
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM daily_weight WHERE user_id='u3'")
    conn.commit()
    conn.close()
    _run("u3")
    assert _nudges("u3") == [], "体重の増減が分からない人には送らない"


def test_not_stacked_on_recent_coach_message(db):
    """直近に別の声かけ（コーチの個別メッセージ）が届いている人には重ねない。"""
    _member("u4")
    ts = datetime.datetime.now(m.JST).isoformat()
    conn = m._get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO coach_messages (user_id,name,reason,message,status,created_at,sent_at) "
                f"VALUES ('u4',NULL,'声かけ','がんばりましょう','sent',{m.PH},{m.PH})", (ts, ts))
    conn.commit()
    conn.close()
    _run("u4")
    assert _nudges("u4") == []


def test_wording_is_gentle():
    """責めない言い方：「強いて言えば」で始め、太った・痩せていない等の言葉を使わない。"""
    t = m._photo_nudge_message(1.0)
    assert "強いて言えば" in t and "ありがとうございます" in t and "無理のない範囲" in t
    for bad in ("痩せていない", "痩せない", "太", "増えて", "サボ", "怠", "ダメ", "失敗"):
        assert bad not in t, f"気を悪くさせる言葉「{bad}」が入っている"
    assert "あまり上がっていない" in m._photo_nudge_message(0.0)
    assert "約1.5枚" in m._photo_nudge_message(1.4)
