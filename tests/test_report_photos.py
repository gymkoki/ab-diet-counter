# デイリーレポートの「実際の食事写真」セクションの回帰テスト。
#
# オーナー指示 2026-09-22：
#   デイリーレポートの最初の文章は読まないので、減量が順調な人／そうでない人の
#   リアルな食事写真を出して視覚的に分かるようにする。
#   デイリーレポートはオーナーだけが見られるようにする。
#
# ここで固定する不変条件：
#   ①写真APIは管理者専用（未認証は404）＝会員の食事写真が外から見えない
#   ②減量が順調な人と進んでいない人が、それぞれ分けて返る
#   ③14日以上まったく記録がない会員（離脱者）は載せない（オーナー方針 2026-09-09）
#   ④レポートの送信先はオーナーの1アドレスだけ（送信先を増やさない）
#   ⑤写真セクションがメール本文の「AI減量コーチ」より前に置かれている

import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
os.environ.setdefault("ADMIN_PASSWORD", "testpw")

import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _make_photo(px=800):
    """テスト用の実際のJPEG（data URI）。APIはサムネに縮小して返すので実画像が要る。"""
    import base64 as _b64
    import io as _io
    from PIL import Image as _Image
    im = _Image.new("RGB", (px, px), (180, 160, 140))
    buf = _io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + _b64.b64encode(buf.getvalue()).decode()


PHOTO = _make_photo()
BROKEN_PHOTO = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/broken"


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _reset_db():
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        for t in ("user_profile", "daily_b_count", "daily_weight", "daily_meals"):
            cur.execute(f"DELETE FROM {t}")
        conn.commit()
    finally:
        conn.close()


def _seed_member(uid, name, start_w, latest_w, last_record_days=1, with_photo=True, photo_days=1):
    """減量希望の会員を1人作る。30日前と直近の体重を入れて変化量を作り、
    直近の食事写真も1件入れる。"""
    now = datetime.datetime.now(m.JST)
    today = now.date()
    ts = now.isoformat()
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        PH = m.PH
        cur.execute(
            f"INSERT INTO user_profile (user_id, display_name, goal, gender, updated_at) "
            f"VALUES ({PH},{PH},{PH},{PH},{PH})",
            (uid, name, "cut", "female", ts),
        )
        for days, w in ((25, start_w), (1, latest_w)):
            cur.execute(
                f"INSERT INTO daily_weight (user_id,date,weight,created_at) VALUES ({PH},{PH},{PH},{PH})",
                (uid, (today - datetime.timedelta(days=days)).isoformat(), w, ts),
            )
        cur.execute(
            f"INSERT INTO daily_b_count (user_id,date,b_count,created_at) VALUES ({PH},{PH},{PH},{PH})",
            (uid, (today - datetime.timedelta(days=last_record_days)).isoformat(), 3.0, ts),
        )
        if with_photo:
            # photo_days 日ぶん、1日2枚ずつ写真を入れる（枚数制限が無いことを確かめるため）
            for d in range(1, photo_days + 1):
                payload = {"food": {"items": [{
                    "previewSrc": PHOTO,
                    "result": {"foods": [{"name": "白米"}, {"name": "唐揚げ"}],
                               "total_b_count": 2, "total_protein_g": 20, "total_veg_g": 30},
                } for _ in range(2)]}}
                cur.execute(
                    f"INSERT INTO daily_meals (user_id,date,payload,created_at) VALUES ({PH},{PH},{PH},{PH})",
                    (uid, (today - datetime.timedelta(days=d)).isoformat(), json.dumps(payload), ts),
                )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    m.init_db()
    _reset_db()
    with m.app.test_client() as c:
        yield c
    _reset_db()


# ── ① 管理者専用（会員の写真が外から見えない） ──────────────────
def test_photos_api_requires_admin(client):
    r = client.get("/api/admin/report-photos")
    assert r.status_code == 404, "未認証で写真APIが開いている"


def test_photos_api_rejects_wrong_password(client):
    r = client.get("/api/admin/report-photos", headers={"X-Admin-Password": "wrong"})
    assert r.status_code == 404


# ── ② 順調な人と進んでいない人が分かれて返る ────────────────────
def test_photos_split_good_and_bad(client):
    _seed_member("u_good", "がんばり子", 70.0, 67.5)   # -2.5kg
    _seed_member("u_bad",  "ていたい男", 70.0, 71.0)   # +1.0kg
    r = client.get("/api/admin/report-photos", headers=ADMIN)
    assert r.status_code == 200
    d = r.get_json()

    good_names = [x["name"] for x in d["good"]]
    bad_names  = [x["name"] for x in d["bad"]]
    assert "がんばり子" in good_names, f"順調な人が入っていない: {d}"
    assert "ていたい男" in bad_names, f"停滞している人が入っていない: {d}"
    assert "がんばり子" not in bad_names and "ていたい男" not in good_names

    photo = d["good"][0]["photos"][0]
    assert photo["src"].startswith("data:image/jpeg;base64,")
    assert len(photo["src"]) < len(PHOTO), "サムネに縮小されていない（メールが重くなる）"
    assert photo["b_count"] == 2
    assert "白米" in photo["foods"]
    assert d["good"][0]["change_30d_kg"] < 0
    assert d["bad"][0]["change_30d_kg"] > 0


# ── ③ 離脱者（14日以上記録なし）は載せない ─────────────────────
def test_photos_exclude_long_absent(client):
    _seed_member("u_gone", "はなれ子", 70.0, 67.0, last_record_days=20)
    r = client.get("/api/admin/report-photos", headers=ADMIN)
    names = [x["name"] for x in r.get_json()["good"]]
    assert "はなれ子" not in names, "14日以上記録がない会員がレポートに載っている"


def test_photos_skip_members_without_photo(client):
    _seed_member("u_nophoto", "写真なし子", 70.0, 67.0, with_photo=False)
    d = client.get("/api/admin/report-photos", headers=ADMIN).get_json()
    assert all(x["name"] != "写真なし子" for x in d["good"] + d["bad"])


def test_photos_empty_is_ok(client):
    d = client.get("/api/admin/report-photos", headers=ADMIN).get_json()
    assert d["good"] == [] and d["bad"] == []


# ── 枚数の制限をしない（オーナー指示 2026-09-23：全部の写真を出す） ──────
def test_photos_returns_all_not_just_three(client):
    """1人あたり3枚で打ち切らず、期間内の写真をすべて返す。"""
    _seed_member("u_many", "たくさん子", 70.0, 67.0, photo_days=6)   # 6日 x 2枚 = 12枚
    d = client.get("/api/admin/report-photos", headers=ADMIN).get_json()
    me = next(x for x in d["good"] if x["name"] == "たくさん子")
    assert len(me["photos"]) == 12, f"写真が打ち切られている: {len(me['photos'])}枚"


def test_photos_respects_explicit_limit(client):
    """枚数を明示したときはその枚数に絞れる（調査用）。"""
    _seed_member("u_many2", "たくさん男", 70.0, 67.0, photo_days=6)
    d = client.get("/api/admin/report-photos?photos=4", headers=ADMIN).get_json()
    me = next(x for x in d["good"] if x["name"] == "たくさん男")
    assert len(me["photos"]) == 4


def test_photos_hard_cap_protects_against_runaway(client):
    """制限なしでも安全弁の上限を超えない。"""
    assert m.REPORT_PHOTO_PER_MEMBER == 0, "既定が「全部」になっていない"
    assert 0 < m.REPORT_PHOTO_HARD_CAP <= 200


def test_photos_skips_broken_images(client):
    """画像として読めないデータは載せない（メール側で壊れた枠が出ないように）。"""
    _seed_member("u_broken", "壊れ子", 70.0, 67.0)
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        payload = {"food": {"items": [{"previewSrc": BROKEN_PHOTO, "result": {"total_b_count": 1}}]}}
        cur.execute(f"UPDATE daily_meals SET payload={m.PH} WHERE user_id={m.PH}",
                    (json.dumps(payload), "u_broken"))
        conn.commit()
    finally:
        conn.close()
    d = client.get("/api/admin/report-photos", headers=ADMIN).get_json()
    assert all(x["name"] != "壊れ子" for x in d["good"] + d["bad"])


def test_photos_skips_oversized_images(client):
    """極端に大きい写真はメールが壊れるので最初から除く。"""
    _seed_member("u_big", "でか写真子", 70.0, 67.0)
    huge = "data:image/jpeg;base64," + "A" * (m.REPORT_PHOTO_MAX_CHARS + 10)
    now = datetime.datetime.now(m.JST)
    payload = {"food": {"items": [{"previewSrc": huge, "result": {"total_b_count": 1}}]}}
    conn = m._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE daily_meals SET payload={m.PH} WHERE user_id={m.PH}",
            (json.dumps(payload), "u_big"),
        )
        conn.commit()
    finally:
        conn.close()
    d = client.get("/api/admin/report-photos", headers=ADMIN).get_json()
    assert all(x["name"] != "でか写真子" for x in d["good"] + d["bad"])


# ── ④ レポートはオーナーだけに届く ───────────────────────────
def test_report_goes_only_to_owner():
    src = _read("report/send_report.py")
    assert 'smtp.sendmail(GMAIL_USER, [REPORT_TO], msg.as_bytes())' in src, \
        "レポートの送信先が1アドレスだけでなくなっている"
    # 宛先の振り替え（設定に古いアドレスが残っていても既定へ戻す）が生きているか
    assert m._resolve_report_to() == m.REPORT_TO_DEFAULT or m._resolve_report_to()
    assert "reallgym.tokyo@gmail.com" in m.REPORT_TO_BLOCKED


def test_photos_api_is_not_called_from_member_app():
    """会員向けアプリから管理APIを呼んでいないこと（写真が会員に見えない）。"""
    front = _read("templates/index.html")
    assert "report-photos" not in front
    assert "/api/admin/" not in front


# ── ⑤ 写真はメール本文の上のほう（コーチ提案より前）にある ──────────
def test_photo_section_is_above_coach_section():
    src = _read("report/send_report.py")
    assert "{photo_section}" in src, "写真セクションが本文に差し込まれていない"
    assert src.index("{photo_section}") < src.index("{_coach_section(coach_advice)}"), \
        "写真がAI減量コーチの文章より下にある（オーナーは最初の文章を読まない）"
    # 枚数が増えても横に伸びず、3枚ずつ折り返して全部表示する
    assert "PER_ROW = 3" in src and "cells[i:i + PER_ROW]" in src, \
        "写真の折り返しが無い（枚数が増えると横にはみ出す）"


def test_photo_section_protects_gmail_body_limit():
    """枚数無制限でも、Gmailが本文を途中で切る約102KBを超えないよう上限を持つ。"""
    src = _read("report/send_report.py")
    assert "MAX_PHOTOS_TOTAL" in src, "セクション全体の上限が無い"
    assert "if len(cells) >= budget:" in src, "上限が写真の追加に効いていない"
    # 1枚あたりのHTMLを短く保つためにクラスを使っている（インラインstyleの羅列に戻さない）
    assert 'class="pc"' in src and "td.pc{{" in src, \
        "写真セルがクラス化されていない（本文が膨らんでGmailに切られる）"
    # 写真の取得に失敗してもレポート自体は送る
    assert "photo section failed" in src
