"""リオールジム会員限定ゲート（合言葉）の回帰テスト。

オーナー指示 2026-09-14：AIの解析コストが高く持続できないため、
2026-09-15 からは合言葉を入れた端末だけがアプリを使えるようにする。

守りたい不変条件：
  ①【最重要】お金がかかる入口（/analyze・/reanalyze・/analyze-text・
     /estimate-nutrition）は、合言葉を通していない端末をサーバー側で必ず止める。
     画面を回避してURLを直接叩かれても課金させない。
  ②止めたときは 403 と unlock_required を返す（画面側が入力を出す目印）
  ③正しい合言葉を入れた端末には通行証が出て、以後は通る
  ④開始日より前は誰も止めない
  ⑤合言葉は3桁しかないので、総当たりは回数制限で止める
  ⑥DBが落ちているときは止めない（会員を締め出さない）
  ⑦他人の通行証・でたらめな通行証では通らない
"""
import io
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as m  # noqa: E402
from conftest import clear_devices, seed_weight, unlock_device  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UID = "member-gate-user"

# お金がかかる入口。ここを1つでも通すとコストが漏れる。
PAID_ENDPOINTS = ("/analyze", "/reanalyze", "/analyze-text", "/estimate-nutrition")


@pytest.fixture
def client(monkeypatch):
    m.init_db()
    m.app.config["TESTING"] = True
    # ゲートを有効にする（conftest が既定で無効にしているため、ここで戻す）
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2020-01-01")
    monkeypatch.setattr(m, "get_client", lambda: object())
    monkeypatch.setattr(m, "create_and_parse", lambda *a, **k: {"foods": [], "total_b_count": 0})
    m._unlock_fails.clear()
    clear_devices()
    seed_weight(UID)          # 体重ゲートで弾かれないようにしておく
    with m.app.test_client() as c:
        yield c


def _png():
    return (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "meal.png")


def _post(c, path, token=None):
    """その入口を叩く。token があれば通行証として付ける。"""
    headers = {"X-Device-Token": token} if token else {}
    data = {"user_id": UID}
    if path == "/analyze":
        data["image"] = _png()
    elif path == "/reanalyze":
        data["correction"] = "白米は75gです"
    elif path == "/analyze-text":
        data["text"] = "焼き鮭定食"
    else:
        data["text"] = "白ごはん"
    return c.post(path, data=data, headers=headers,
                  content_type="multipart/form-data")


# ── ①② サーバー側で必ず止まること ──────────────────────────────
@pytest.mark.parametrize("path", PAID_ENDPOINTS)
def test_paid_endpoint_is_blocked_without_password(client, path):
    r = _post(client, path)
    assert r.status_code == 403, f"{path} が合言葉なしで通っています（課金が漏れます）"
    assert r.get_json().get("unlock_required") is True


@pytest.mark.parametrize("path", PAID_ENDPOINTS)
def test_paid_endpoint_works_after_unlock(client, path):
    token = unlock_device(UID)
    r = _post(client, path, token=token)
    assert r.status_code != 403, f"{path} が認証済みなのに止められています"


# ── ③ 合言葉から通行証が出る ────────────────────────────────────
def test_unlock_issues_a_token(client):
    r = client.post("/api/unlock", json={"user_id": UID, "password": "860"})
    assert r.status_code == 200
    token = r.get_json()["token"]
    assert token
    # 出た通行証で解析が通る
    assert _post(client, "/analyze", token=token).status_code != 403
    # 状態確認にも反映される
    d = client.get(f"/api/member-status?user_id={UID}&token={token}").get_json()
    assert d["gate_active"] is True and d["unlocked"] is True


def test_wrong_password_is_rejected(client):
    r = client.post("/api/unlock", json={"user_id": UID, "password": "123"})
    assert r.status_code == 401
    assert "token" not in r.get_json()
    assert _post(client, "/analyze").status_code == 403


def test_password_can_be_changed_from_dashboard(client):
    admin = {"X-Admin-Password": m.ADMIN_PASSWORD}
    assert client.post("/api/admin/member-gate", json={"password": "7391"},
                       headers=admin).status_code == 200
    assert client.post("/api/unlock", json={"user_id": UID, "password": "860"}).status_code == 401
    assert client.post("/api/unlock", json={"user_id": UID, "password": "7391"}).status_code == 200
    m._set_setting(m.MEMBER_PASSWORD_KEY, "")   # 後片付け


# ── ④ 開始日より前は止めない ────────────────────────────────────
def test_gate_is_off_before_start_date(client, monkeypatch):
    monkeypatch.setattr(m, "MEMBER_GATE_START", "2099-01-01")
    for path in PAID_ENDPOINTS:
        assert _post(client, path).status_code != 403, f"{path} が開始前に止まっています"
    assert client.get(f"/api/member-status?user_id={UID}").get_json()["gate_active"] is False


def test_broken_start_date_does_not_lock_everyone_out(client, monkeypatch):
    """日付の設定ミスで会員全員を締め出さないこと。"""
    monkeypatch.setattr(m, "MEMBER_GATE_START", "これは日付ではない")
    assert m._member_gate_active() is False
    assert _post(client, "/analyze").status_code != 403


# ── ⑤ 総当たり対策 ──────────────────────────────────────────────
def test_repeated_wrong_passwords_get_locked_out(client):
    """3桁＝1000通りしかないため、回数制限が無いと簡単に破られる。"""
    last = None
    for _ in range(m.MEMBER_UNLOCK_MAX_FAILS + 2):
        last = client.post("/api/unlock", json={"user_id": UID, "password": "000"})
    assert last.status_code == 429, "何回間違えても試せる状態です（総当たりで破られます）"
    # ロック中は正しい合言葉でも通さない
    assert client.post("/api/unlock", json={"user_id": UID, "password": "860"}).status_code == 429


def test_successful_unlock_clears_the_failure_count(client):
    for _ in range(3):
        client.post("/api/unlock", json={"user_id": UID, "password": "000"})
    assert client.post("/api/unlock", json={"user_id": UID, "password": "860"}).status_code == 200
    assert not m._unlock_fails.get("127.0.0.1")


# ── ⑥⑦ 例外的な状況 ────────────────────────────────────────────
def test_db_failure_does_not_block(client, monkeypatch):
    """DBが落ちているときに会員を締め出さない（体重ゲートと同じ方針）。"""
    def _boom(uid):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "_device_token", _boom)
    assert m._is_unlocked(UID, "whatever") is True


def test_other_devices_token_does_not_work(client):
    unlock_device("someone-else", token="stolen-token")
    assert _post(client, "/analyze", token="stolen-token").status_code == 403
    assert _post(client, "/analyze", token="でたらめ").status_code == 403


def test_unlock_requires_user_id(client):
    assert client.post("/api/unlock", json={"password": "860"}).status_code == 400


def test_revoke_all_forces_everyone_to_re_enter(client):
    token = unlock_device(UID)
    assert _post(client, "/analyze", token=token).status_code != 403
    admin = {"X-Admin-Password": m.ADMIN_PASSWORD}
    assert client.post("/api/admin/member-gate/revoke-all", headers=admin).status_code == 200
    assert _post(client, "/analyze", token=token).status_code == 403


def test_admin_endpoints_require_auth(client):
    assert client.get("/api/admin/member-gate").status_code == 404
    assert client.post("/api/admin/member-gate/revoke-all").status_code == 404


# ── 画面側 ──────────────────────────────────────────────────────
def _html(name="index.html"):
    with open(os.path.join(ROOT, "templates", name), encoding="utf-8") as f:
        return f.read()


def test_frontend_has_a_blocking_gate():
    html = _html()
    m_ = re.search(r'<div id="member-gate-overlay"([^>]*)>', html)
    assert m_, "合言葉の入力画面がありません"
    style = m_.group(1)
    assert "display:none" in style, "既定で表示されてしまいます"
    # 他のどのモーダルよりも前面に出ること
    zs = [int(z) for z in re.findall(r"z-index:(\d+)", html)]
    mine = int(re.search(r"z-index:(\d+)", style).group(1))
    assert mine == max(zs), "他のモーダルに隠れる可能性があります"
    # 閉じる手段を置かない（閉じられたら意味がない）
    block = html[html.index('id="member-gate-overlay"'):]
    block = block[:block.index("</div>\n</div>")]
    assert "closeMemberGate()" not in block, "自分で閉じられてしまいます"


def test_frontend_sends_the_token_on_paid_requests():
    html = _html()
    assert "memberHeaders()" in html
    # 解析3種が通る共通の fetch と、栄養推定の両方に付いていること
    fn = re.search(r"function _fetchWithTimeout\([^)]*\)\s*\{(.*?)\n\}", html, re.S)
    assert fn and "memberHeaders()" in fn.group(1), "解析リクエストに通行証が付いていません"
    assert re.search(r"/estimate-nutrition'[^;]*memberHeaders\(\)", html, re.S), \
        "栄養推定に通行証が付いていません"


def test_frontend_reacts_to_server_block():
    """サーバーが403で断ったら、素っ気ないエラーではなく入力画面を出すこと。"""
    html = _html()
    assert "_handledMemberGate" in html
    assert html.count("_handledMemberGate(data)") >= 4   # 定義1 + 3か所の呼び出し
    assert "checkMemberGate()" in html, "起動時の確認が入っていません"


def test_dashboard_can_manage_the_password():
    admin = _html("admin.html")
    assert "/api/admin/member-gate" in admin
    assert "revokeAllDevices" in admin
    assert "loadMemberGate()" in admin


def test_unlock_survives_settings_lookup_failure(client, monkeypatch):
    """DBから合言葉を読めなくても既定値で認証できること。

    ここで例外が出ると /api/unlock が500になり、会員が誰ひとり
    認証できない＝アプリが完全に使えない状態になる。
    """
    def _boom(*a, **k):
        raise RuntimeError("no such table: settings")
    monkeypatch.setattr(m, "_get_setting", _boom)
    assert m._member_password() == m.MEMBER_PASSWORD_DEFAULT
    r = client.post("/api/unlock", json={"user_id": UID, "password": m.MEMBER_PASSWORD_DEFAULT})
    assert r.status_code == 200 and r.get_json().get("token")
