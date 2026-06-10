import os
import io
import base64
import json
import datetime
import threading
from flask import Flask, render_template, request, jsonify
import anthropic
from dotenv import load_dotenv, set_key

try:
    from PIL import Image
except ImportError:
    Image = None

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH, override=True)
app = Flask(__name__)

# ── 画像の縮小・圧縮（AI分析の高速化）────────────────────
# Claude Visionは画像サイズが大きいほど処理（トークン化）に時間がかかるため、
# 分析前に長辺を最大1024pxへ縮小し、JPEGで圧縮してから送信する。
MAX_IMAGE_DIMENSION = 1024
JPEG_QUALITY = 85


def prepare_image_for_api(image_data: bytes, fallback_media_type: str):
    """画像を縮小・圧縮し、(base64文字列, media_type) を返す。"""
    if Image is None:
        return base64.standard_b64encode(image_data).decode("utf-8"), fallback_media_type

    try:
        img = Image.open(io.BytesIO(image_data))
        img = img.convert("RGB")  # EXIF回転やCMYK/PNG透過を正規化

        w, h = img.size
        longest = max(w, h)
        if longest > MAX_IMAGE_DIMENSION:
            scale = MAX_IMAGE_DIMENSION / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return base64.standard_b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"
    except Exception:
        # 万が一画像処理に失敗した場合は元画像をそのまま使う
        return base64.standard_b64encode(image_data).decode("utf-8"), fallback_media_type

# ── 利用ログDB（PostgreSQL 優先、なければ SQLite）──────────
_db_lock = threading.Lock()
JST = datetime.timezone(datetime.timedelta(hours=9))

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
if _DATABASE_URL:
    # Render は "postgres://" で渡してくる場合があるので修正
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
    import psycopg2
    USE_PG = True
    PH = "%s"          # psycopg2 のプレースホルダ
else:
    import sqlite3
    _DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.db")
    USE_PG = False
    PH = "?"           # sqlite3 のプレースホルダ


def _get_conn():
    if USE_PG:
        return psycopg2.connect(_DATABASE_URL)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if USE_PG:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usage_log (
                    id         SERIAL PRIMARY KEY,
                    user_id    TEXT   NOT NULL,
                    created_at TEXT   NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_b_count (
                    id         SERIAL PRIMARY KEY,
                    user_id    TEXT   NOT NULL,
                    date       TEXT   NOT NULL,
                    b_count    REAL   NOT NULL,
                    created_at TEXT   NOT NULL,
                    UNIQUE(user_id, date)
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usage_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT    NOT NULL,
                    created_at TEXT    NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_b_count (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT    NOT NULL,
                    date       TEXT    NOT NULL,
                    b_count    REAL    NOT NULL,
                    created_at TEXT    NOT NULL,
                    UNIQUE(user_id, date)
                )
            """)
        conn.commit()
    finally:
        conn.close()


init_db()

ANALYSIS_PROMPT = """この写真に写っている食事・食材をすべて特定し、ABダイエットのルールに基づいてBカウントを計算してください。

━━━━━━━━━━━━━━━━━━━━━━━
■ 食材の分類（1人前あたりのkcalで判定）
━━━━━━━━━━━━━━━━━━━━━━━

【A食材】― 1人前120kcal未満 → いくら食べてもBカウント0
- 野菜全般（生・加熱・炒め・茹でいずれも）
- 果物全般、きのこ類、海藻類
- 豆腐・こんにゃく・大豆製品（納豆を除く）
- 調味料少量（塩・醤油・酢・ハーブ・スパイスなど）
- お茶・ブラックコーヒー・水
- プロテインドリンク（牛乳で割っていないもの）

【Good B食材】― 1人前120kcal以上200kcal未満
- 納豆（1人前2パック≒140kcal）
- さつまいも（100g≒130kcal）
- オートミール（30〜40g≒120〜150kcal）
- 全粒粉パン1枚（≒120〜140kcal）
- 食パン1枚（≒120kcal）
- バナナ（1〜2本≒80〜180kcal　※量で判断）
- トウモロコシ（1本≒150kcal）
※ 肉・魚・乳製品は下記「タンパク質食材」で判定

【B食材】― 1人前200kcal以上
- 白米・チャーハン（1杯150g≒250kcal）
- 食パン2枚（≒250kcal）
- 麺類・マカロニ・パスタ（1人前≒270〜350kcal）
- 油・バター・ラード（1人前＝大さじ2≒240kcal）※下記「油の特別ルール」参照
- スイーツ・菓子（1人前200kcal以上）
- ベシャメルソース・クリームソース（1人前150g≒200kcal）
- 牛乳（1人前400ml≒250kcal）
- アルコール（1人前200kcal以上）
※ 肉・魚・乳製品は下記「タンパク質食材」で判定

━━━━━━━━━━━━━━━━━━━━━━━
■ タンパク質食材（肉・魚・豆・乳製品）の分類
  1人前＝100g 基準
━━━━━━━━━━━━━━━━━━━━━━━

【A食材：100gあたり120kcal未満】
- 鶏むね肉（皮なし）・ささみ：約100〜108kcal
- マグロ赤身：約115kcal
- タラ・ヒラメ・カレイ・アジなど白身・低脂質魚：約70〜100kcal
- えび・いか・たこ・ホタテ・貝類：約80〜100kcal
- 木綿・絹豆腐：約56〜70kcal

【Good B食材：100gあたり120kcal以上200kcal未満】
- 皮付き鶏もも肉：約160kcal
- 豚もも肉（赤身）：約128kcal
- 牛もも肉（赤身）：約140kcal
- ツナ缶（油漬け、汁切り）：約130kcal
- 鯖缶（水煮）：約130kcal
- チーズ（30gで約110〜120kcal　※量次第）

【B食材：100gあたり200kcal以上】
- 鮭・サーモン：約200〜220kcal
- ブリ（生・焼き）：約257kcal
- サバ（生・焼き）：約247kcal
- サンマ（生・焼き）：約318kcal
- ウナギ蒲焼き：約255kcal
- ひき肉（合挽き・豚・牛）：約200〜250kcal
- 豚バラ肉：約386kcal
- 牛バラ・サーロイン・リブロース：約200kcal以上

━━━━━━━━━━━━━━━━━━━━━━━
■ Bカウントの計算方法【最重要】
━━━━━━━━━━━━━━━━━━━━━━━

料理名でカウントせず、食材ごとに分解して個別に判定する。

【Good B食材のカウント】
- 1人前      → B 0.5
- 半人前      → B 0.25
- 半人前未満  → B 0（ノーカウント）

【B食材のカウント】
- 1人前      → B 1
- 半人前      → B 0.5
- 半人前未満  → B 0（ノーカウント）

━━━━━━━━━━━━━━━━━━━━━━━
■ 具体例
━━━━━━━━━━━━━━━━━━━━━━━

【グラタン1人前】
① マカロニ（底層にたっぷり、1人前≒280kcal）       → B食材 1人前 → B1
② ベシャメルソース＋チーズ（合わせて1人前≒200kcal）→ B食材 1人前 → B1
③ ひき肉（少量の具材、半人前未満）                  → B食材 だがノーカウント → B0
④ 野菜（玉ねぎ・ほうれん草など）                    → A食材 → B0
→ 合計 B2

【ハンバーグ定食】
① 白米1杯（150g≒250kcal）     → B食材 1人前 → B1
② ひき肉1個（100g≒250kcal）   → B食材 1人前 → B1
③ 炒め油（小さじ1〜2程度、大さじ1未満）→ B食材だが半人前未満 → B0
④ 野菜の付け合わせ              → A食材 → B0
→ 合計 B2

【ラーメン】
① 麺1人前（≒270kcal）           → B食材 1人前 → B1
② チャーシュー1〜2枚（50g≒半人前）→ B食材 半人前 → B0.5
③ スープの油（少量）              → B0
→ 合計 B1.5

【焼肉定食】
① 白米1杯                        → B食材 1人前 → B1
② 牛バラ1人前（100g≒317kcal）   → B食材 1人前 → B1
→ 合計 B2

【鶏もも唐揚げ定食】
① 唐揚げ（鶏もも100g≒160kcal）  → Good B 1人前 → B0.5
② 吸収油（100gあたり約15g≒135kcal）→ Good B相当 → B0.5
③ 白米1杯                         → B食材 1人前 → B1
→ 合計 B2

【焼き豚（チャーシュー）30〜40g】
- 豚もも：Good B食材（1人前100g≒128kcal）
- 写真の量30〜40gは半人前未満 → B0（ノーカウント）

━━━━━━━━━━━━━━━━━━━━━━━
■ ドリンクのルール（500ml換算）
━━━━━━━━━━━━━━━━━━━━━━━
ジュース・乳飲料・炭酸飲料などは500mlを1人前として判定する。
- 500mlあたり120kcal未満       → A食材 → B0
- 500mlあたり120kcal以上200kcal未満 → Good B食材 → 量でカウント
- 500mlあたり200kcal以上       → B食材 → 量でカウント
100ml程度（半人前未満）の摂取はノーカウント。
プロテインドリンク（牛乳で割らないもの）はA食材。

━━━━━━━━━━━━━━━━━━━━━━━
■ 油・バターの特別ルール【B食材・大さじ基準】
━━━━━━━━━━━━━━━━━━━━━━━
油・バター・ラードはすべてB食材。1人前＝大さじ2（約240kcal）。

- 大さじ2以上（1人前）  → B1
- 大さじ1（半人前）     → B0.5
- 大さじ1未満（小さじ2以下など）→ B0（ノーカウント）

【参考：よくある量】
- 炒め物の油（小さじ1〜2）     → 大さじ1未満 → B0
- ドレッシングたっぷり（大さじ1）→ B0.5
- 揚げ物の吸収油（100g分 ≒ 大さじ1相当、約135kcal）→ B0.5
- バターソテー（大さじ2以上）   → B1

━━━━━━━━━━━━━━━━━━━━━━━
■ 揚げ物のルール（主食材＋吸収油を個別カウント）
━━━━━━━━━━━━━━━━━━━━━━━
- 主食材：A / Good B / B食材かを判定して量でカウント
- 衣（小麦粉・卵・パン粉）：常にノーカウント
- 吸収油：食材重量の約20%（油1g≒9kcal）→ 上記「油の特別ルール」で判定
    100g揚げ物 → 吸油約15g（≒大さじ1相当）→ B食材 半人前 → B0.5
    200g以上の揚げ物 → 吸油約30g以上（≒大さじ2以上）→ B食材 1人前 → B1

必ず以下のJSON形式のみで回答してください。JSONの前後に説明文やコードブロックは不要です：
{
  "foods": [
    {
      "name": "食材名（具体的に）",
      "category": "A",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量（例：たっぷり、1人前、少量など）",
      "b_count": 0,
      "reason": "A食材のため（1人前約○kcal、120kcal未満）"
    },
    {
      "name": "食材名（具体的に）",
      "category": "Good B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量（例：1人前、半人前、半人前未満など）",
      "b_count": 0.5,
      "reason": "Good B食材（1人前約○kcal）、1人前のためB0.5"
    },
    {
      "name": "食材名（具体的に）",
      "category": "Good B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "半人前",
      "b_count": 0.25,
      "reason": "Good B食材（1人前約○kcal）、半人前のためB0.25"
    },
    {
      "name": "食材名（具体的に）",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "1人前",
      "b_count": 1,
      "reason": "B食材（1人前約○kcal）、1人前のためB1"
    },
    {
      "name": "食材名（具体的に）",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "半人前",
      "b_count": 0.5,
      "reason": "B食材（1人前約○kcal）、半人前のためB0.5"
    }
  ],
  "total_b_count": 合計Bカウント（数値）,
  "advice": "このメニューをABダイエット観点でのワンポイントアドバイス（1〜2文）"
}"""


def get_client():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/admin")
def admin():
    return render_template("admin.html")


@app.route("/api/status")
def status():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return jsonify({"configured": bool(key)})


@app.route("/ping")
def ping():
    """Render無料プランのスリープ防止用エンドポイント（外部監視サービスからpingされる）"""
    return "ok", 200


@app.route("/api/setup", methods=["POST"])
def setup():
    data = request.get_json()
    key = (data or {}).get("api_key", "").strip()
    if not key or not key.startswith("sk-"):
        return jsonify({"error": "正しいAPIキーを入力してください（sk- で始まるもの）"}), 400

    if not os.path.exists(ENV_PATH):
        open(ENV_PATH, "w").close()
    set_key(ENV_PATH, "ANTHROPIC_API_KEY", key)
    os.environ["ANTHROPIC_API_KEY"] = key

    return jsonify({"ok": True})


@app.route("/api/stats")
def get_stats():
    uid = request.args.get("user_id", "")
    now = datetime.datetime.now(JST)
    today_start = now.strftime("%Y-%m-%dT00:00:00")
    month_start = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT COUNT(DISTINCT user_id) FROM usage_log WHERE created_at >= {PH}",
            (today_start,)
        )
        today_users = cur.fetchone()[0]

        cur.execute(
            f"""SELECT user_id, COUNT(*) cnt FROM usage_log
               WHERE created_at >= {PH}
               GROUP BY user_id ORDER BY cnt DESC""",
            (month_start,)
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    ranking, my_rank, my_count = [], None, 0
    for i, (row_uid, cnt) in enumerate(rows):
        is_me = row_uid == uid
        ranking.append({"rank": i + 1, "count": cnt, "is_me": is_me})
        if is_me:
            my_rank, my_count = i + 1, cnt

    return jsonify({
        "today_users": today_users,
        "ranking": ranking[:10],
        "my_rank": my_rank,
        "my_count": my_count,
        "total_users": len(rows),
    })


@app.route("/api/daily-b", methods=["POST"])
def post_daily_b():
    """今日のBカウントを記録（全食事完了時に呼ばれる）"""
    data = request.get_json()
    uid     = (data or {}).get("user_id", "").strip()
    b_count = (data or {}).get("b_count", 0)
    date    = (data or {}).get("date", "")   # YYYY-MM-DD (JST)
    if not uid or not date:
        return jsonify({"error": "パラメータ不足"}), 400

    ts = datetime.datetime.now(JST).isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            if USE_PG:
                cur.execute(
                    """INSERT INTO daily_b_count (user_id, date, b_count, created_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, date) DO UPDATE SET b_count=%s, created_at=%s""",
                    (uid, date, b_count, ts, b_count, ts)
                )
            else:
                cur.execute(
                    """INSERT INTO daily_b_count (user_id, date, b_count, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(user_id, date) DO UPDATE SET b_count=excluded.b_count, created_at=excluded.created_at""",
                    (uid, date, b_count, ts)
                )
            conn.commit()
        finally:
            conn.close()
    return jsonify({"ok": True})


@app.route("/api/weekly-b")
def get_weekly_b():
    """今週（月〜日）のBカウント合計と日数を返す"""
    uid = request.args.get("user_id", "")
    if not uid:
        return jsonify({"week_total": 0, "days_count": 0})

    now = datetime.datetime.now(JST)
    # 今週の月曜日を計算
    week_start = (now - datetime.timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    week_end   = (now + datetime.timedelta(days=6 - now.weekday())).strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT date, b_count FROM daily_b_count
               WHERE user_id={PH} AND date>={PH} AND date<={PH}
               ORDER BY date""",
            (uid, week_start, week_end)
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    week_total = sum(r[1] for r in rows)
    days_count = len(rows)
    daily = [{"date": r[0], "b_count": r[1]} for r in rows]
    return jsonify({
        "week_total": week_total,
        "days_count": days_count,
        "daily": daily,
        "week_start": week_start,
        "week_end": week_end,
    })


@app.route("/analyze", methods=["POST"])
def analyze():
    client = get_client()
    if client is None:
        return jsonify({"error": "APIキーが設定されていません。設定画面から登録してください。"}), 401

    if "image" not in request.files:
        return jsonify({"error": "画像が見つかりません"}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"error": "ファイルが選択されていません"}), 400

    # 利用ログ記録
    uid = request.form.get("user_id", "")
    if uid:
        ts = datetime.datetime.now(JST).isoformat()
        with _db_lock:
            conn = _get_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    f"INSERT INTO usage_log (user_id, created_at) VALUES ({PH}, {PH})",
                    (uid, ts)
                )
                conn.commit()
            finally:
                conn.close()

    image_data = file.read()

    media_type = file.content_type
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media_type = "image/jpeg"

    base64_image, media_type = prepare_image_for_api(image_data, media_type)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64_image,
                            },
                        },
                        {
                            "type": "text",
                            "text": ANALYSIS_PROMPT,
                        },
                    ],
                }
            ],
        )

        result_text = response.content[0].text.strip()

        if result_text.startswith("```"):
            lines = result_text.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            result_text = "\n".join(lines)

        result = json.loads(result_text)
        return jsonify(result)

    except json.JSONDecodeError as e:
        return jsonify({"error": f"分析結果の解析に失敗しました。もう一度お試しください。({e})"}), 500
    except anthropic.AuthenticationError:
        return jsonify({"error": "APIキーが無効です。設定画面で正しいキーを入力してください。"}), 401
    except anthropic.APIError as e:
        return jsonify({"error": f"AI分析中にエラーが発生しました: {e}"}), 500


@app.route("/reanalyze", methods=["POST"])
def reanalyze():
    client = get_client()
    if client is None:
        return jsonify({"error": "APIキーが設定されていません。"}), 401

    if "image" not in request.files:
        return jsonify({"error": "画像が見つかりません"}), 400

    file = request.files["image"]
    correction = request.form.get("correction", "").strip()
    if not correction:
        return jsonify({"error": "補足情報を入力してください"}), 400

    image_data = file.read()
    media_type = file.content_type
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media_type = "image/jpeg"

    base64_image, media_type = prepare_image_for_api(image_data, media_type)

    prompt_with_correction = ANALYSIS_PROMPT + f"""

━━━━━━━━━━━━━━━━━━━━━━━
【ユーザーからの補足・訂正情報】
{correction}

この補足情報を最優先して、上記のABダイエットルールに基づき再計算してください。
━━━━━━━━━━━━━━━━━━━━━━━"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64_image,
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt_with_correction,
                        },
                    ],
                }
            ],
        )

        result_text = response.content[0].text.strip()
        if result_text.startswith("```"):
            lines = result_text.split("\n")[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            result_text = "\n".join(lines)

        result = json.loads(result_text)
        return jsonify(result)

    except json.JSONDecodeError as e:
        return jsonify({"error": f"分析結果の解析に失敗しました。({e})"}), 500
    except anthropic.AuthenticationError:
        return jsonify({"error": "APIキーが無効です。"}), 401
    except anthropic.APIError as e:
        return jsonify({"error": f"AI分析中にエラーが発生しました: {e}"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
