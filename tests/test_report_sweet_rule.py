"""デイリーレポート「🍬 お菓子・スイーツ・ジュースの特別ルール」の回帰テスト。

オーナー依頼 2026-10-07：
  お菓子の特別ルール（1日の合計がB1などになる）は、毎日何回くらい適用されているか。
  これはデイリーレポートで知りたい。

ルールは1人1日1回まで（合計120〜200kcalで+0.5、200kcal超で+1）なので、
「適用された人数」＝「適用回数」としてレポートに出す。人数だけで名前は出さない。
"""
import datetime
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "report"))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy")
import app as m  # noqa: E402

ADMIN = {"X-Admin-Password": m.ADMIN_PASSWORD}


def _sweet(kcal, b=0):
    return {"name": "お菓子", "category": "B", "b_count": b, "kcal_per_serving": kcal, "sweet_kcal": kcal}


def _day(*foods):
    return json.dumps({"food": {"items": [{"result": {"foods": list(foods),
                                                      "total_b_count": sum(f["b_count"] for f in foods)}}]}})


def test_day_summary_keeps_sweet_bonus():
    s = m._summarize_day_meals(_day(_sweet(60), _sweet(84)))      # 144kcal → +0.5
    assert s["sweet_bonus"] == 0.5 and s["sweet_kcal"] == 144
    s = m._summarize_day_meals(_day(_sweet(150), _sweet(90)))     # 240kcal → +1
    assert s["sweet_bonus"] == 1.0
    s = m._summarize_day_meals(_day(_sweet(50)))                  # 50kcal → なし
    assert s["sweet_bonus"] == 0.0


@pytest.fixture
def seeded():
    m.init_db()
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("daily_meals", "daily_nutrition"):
        cur.execute(f"DELETE FROM {t}")
    today = datetime.datetime.now(m.JST).date()
    y = (today - datetime.timedelta(days=1)).isoformat()
    d2 = (today - datetime.timedelta(days=2)).isoformat()
    rows = [
        ("u-half", y, _day(_sweet(60), _sweet(84))),     # +0.5
        ("u-full", y, _day(_sweet(150), _sweet(90))),    # +1
        ("u-full2", y, _day(_sweet(250))),               # +1
        ("u-none", y, _day(_sweet(50))),                 # なし
        ("u-plain", y, _day({"name": "白米", "category": "B", "b_count": 1, "kcal_per_serving": 250})),
        ("u-half", d2, _day(_sweet(130))),               # 前日：+0.5
    ]
    for uid, d, p in rows:
        cur.execute(f"INSERT INTO daily_meals (user_id,date,payload,created_at) "
                    f"VALUES ({m.PH},{m.PH},{m.PH},{m.PH})", (uid, d, p, d))
    conn.commit()
    conn.close()
    yield y
    conn = m._get_conn()
    cur = conn.cursor()
    for t in ("daily_meals", "daily_nutrition"):
        cur.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()


def test_stats_count_people_per_day(seeded):
    sr = m._sweet_rule_stats(seeded)
    day = sr["day"]
    assert day["measured"] == 5, "記録した人数（お菓子が無い人も含む）"
    assert day["half"] == 1 and day["full"] == 2 and day["applied"] == 3
    assert day["rate"] == 60
    assert len(sr["trend"]) == m.SWEET_RULE_TREND_DAYS and sr["trend"][-1]["date"] == seeded
    prev = sr["trend"][-2]
    assert prev["applied"] == 1 and prev["half"] == 1
    assert "names" not in json.dumps(sr) and "u-half" not in json.dumps(sr), "会員を特定できる情報を出さない"


def test_days_before_the_feature_are_unmeasured(seeded):
    """sweet_bonus が無い（導入前の）行は「未計測」として数えない。"""
    m._sweet_rule_stats(seeded)    # まず埋める
    conn = m._get_conn()
    cur = conn.cursor()
    old = (datetime.date.fromisoformat(seeded) - datetime.timedelta(days=4)).isoformat()
    cur.execute(f"INSERT INTO daily_nutrition (user_id,date,meal_count,protein_g,veg_g,created_at) "
                f"VALUES ({m.PH},{m.PH},3,50,100,'x')", ("u-old", old))
    conn.commit()
    conn.close()
    sr = m._sweet_rule_stats(seeded)
    rec = next(t for t in sr["trend"] if t["date"] == old)
    assert rec["recorded"] == 1 and rec["measured"] == 0 and rec["rate"] is None


def test_report_data_includes_sweet_rule(seeded):
    with m.app.test_client() as c:
        r = c.get("/api/admin/report-data", headers=ADMIN)
        assert r.status_code == 200
        sr = r.get_json()["sweet_rule"]
        assert sr["date"] == seeded and sr["day"]["applied"] == 3


def test_email_section_renders():
    pytest.importorskip("requests", reason="requests 未インストール")
    pytest.importorskip("matplotlib", reason="matplotlib 未インストール")
    import send_report as sr_mod
    sr = {"date": "2026-10-06", "half_kcal": 120, "full_kcal": 200,
          "day": {"date": "2026-10-06", "recorded": 5, "measured": 5, "applied": 3, "half": 1, "full": 2, "rate": 60},
          "trend": [{"date": "2026-10-04", "recorded": 0, "measured": 0, "applied": 0, "half": 0, "full": 0, "rate": None},
                    {"date": "2026-10-05", "recorded": 2, "measured": 0, "applied": 0, "half": 0, "full": 0, "rate": None},
                    {"date": "2026-10-06", "recorded": 5, "measured": 5, "applied": 3, "half": 1, "full": 2, "rate": 60}]}
    html = sr_mod._sweet_rule_section(sr)
    assert "お菓子・スイーツ・ジュースの特別ルール" in html
    assert "3人" in html and "記録した5人中・60%" in html
    assert "1人1日1回まで" in html
    assert "未計測" in html, "記録はあるが導入前の日"
    assert "記録なし" in html, "記録が無い日"
    assert sr_mod._sweet_rule_section(None) == "", "データが無い日はセクションごと出さない"


def test_section_is_wired_into_report():
    src = open(os.path.join(ROOT, "report", "send_report.py"), encoding="utf-8").read()
    assert '{_sweet_rule_section(data.get("sweet_rule"))}' in src
