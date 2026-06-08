import os
import base64
import json
import datetime
import threading
from flask import Flask, render_template, request, jsonify
import anthropic
from dotenv import load_dotenv, set_key

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH, override=True)
app = Flask(__name__)

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
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usage_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT    NOT NULL,
                    created_at TEXT    NOT NULL
                )
            """)
        conn.commit()
    finally:
        conn.close()


init_db()

ANALYSIS_PROMPT = """この写真に写っている食事・食材をすべて特定し、ABダイエットのルールに基づいてBカウントを計算してください。

━━━━━━━━━━━━━━━━━━━━━━━
■ 基本ルール
━━━━━━━━━━━━━━━━━━━━━━━

【A食材】― どれだけ食べてもBカウント0
- 野菜全般（生・加熱・炒め・茹でいずれも）
- 果物全般、きのこ類、海藻類
- 豆腐・こんにゃく・大豆製品（納豆を除く）
- 調味料少量（塩・醤油・酢・ハーブ・スパイスなど）
- お茶・ブラックコーヒー・水
- プロテインドリンク（牛乳で割っていないもの）

【B食材】― 1人前あたり120kcal以上の食材
以下はすべてB食材。Bカウントは「何人前食べたか」で決まる。
- 白米・チャーハン（1杯150g≒250kcal）
- パン（食パン1枚≒120kcal、2枚≒250kcal）
- 麺類（1人前≒270〜350kcal）
- マカロニ・パスタ（1人前乾燥80g≒280kcal）
- 油・バター（大さじ1強≒120kcal）
- スイーツ・菓子（1人前120kcal以上）
- 牛乳（1人前200ml≒130kcal）
- チーズ（1人前30g≒110〜120kcal　※まとめ使いで判断）
- ベシャメルソース・クリームソース（1人前150g≒200kcal）
- 納豆（1人前2パック≒140kcal）
- さつまいも・トウモロコシ・オートミール・バナナ（1人前≒120〜200kcal）

【肉・魚・豆・乳製品のB食材判定】
1人前＝100gを基準に、100gあたり120kcal以上ならB食材。
  ▶ A食材（100gあたり120kcal未満）
    - 鶏むね肉・ささみ：約100〜108kcal
    - マグロ赤身：約115kcal
    - タラ・ヒラメ・カレイ・アジなど白身・低脂質魚：約70〜100kcal
    - えび・いか・たこ・ホタテ・貝類：約80〜100kcal
    - 木綿・絹豆腐：約56〜70kcal
  ▶ B食材（100gあたり120kcal以上）
    - 皮付き鶏もも肉：約160kcal
    - 豚もも・牛もも（赤身）：約128〜140kcal
    - 鮭・サーモン：約200〜220kcal
    - ブリ・サバ・サンマ：約247〜318kcal
    - ウナギ蒲焼き：約255kcal
    - 豚バラ：約386kcal
    - 牛バラ・サーロイン：約200kcal以上
    - ひき肉（合挽き・豚・牛）：約200〜250kcal
    - ツナ缶（油漬け）・鯖缶（水煮）：約130kcal

━━━━━━━━━━━━━━━━━━━━━━━
■ Bカウントの計算方法【最重要】
━━━━━━━━━━━━━━━━━━━━━━━

B食材ごとに「写真に写っている量が何人前か」を推定し、以下で判定する：

  - 1人前以上       → B1
  - 約0.5人前       → B0.5
  - 0.5人前未満     → B0（ノーカウント）

「料理名」でカウントせず、必ずB食材ごとに分解して個別判定すること。

【具体例：グラタン1人前】
① マカロニ（底層にたっぷり、1人前相当≒280kcal）  → B食材1人前 → B1
② ベシャメルソース＋チーズ（表面を覆う量、合わせて1人前相当≒250〜300kcal）→ B食材1人前 → B1
③ ひき肉（少量・具材程度、0.5人前未満）           → B食材だがノーカウント → B0
④ 野菜（玉ねぎ・ほうれん草など）                  → A食材 → B0
→ 合計 B2

【具体例：ハンバーグ定食】
① 白米1杯（150g≒250kcal）    → B1
② ひき肉（1個100g≒250kcal）  → B1
③ 炒め油・調味料（少量）      → B0
④ 野菜の付け合わせ            → A食材 → B0
→ 合計 B2

【具体例：ラーメン】
① 麺1人前（≒270kcal）                  → B1
② チャーシュー1〜2枚（50g程度、0.5人前）→ B0.5
③ スープの油・ラード（少量）             → B0
→ 合計 B1.5

【具体例：焼肉定食】
① 白米1杯 → B1
② 牛バラ1人前（100g≒317kcal） → B1
③ 焼き油（少量） → B0
→ 合計 B2

━━━━━━━━━━━━━━━━━━━━━━━
■ 揚げ物のルール
━━━━━━━━━━━━━━━━━━━━━━━
揚げ物は「主食材」と「吸収油」を別々にカウントする。

- 主食材：A食材かB食材かを判定し、量で判定
- 衣（小麦粉・卵・パン粉）：常にノーカウント
- 吸収油：食材重量の約20%が油吸収（油1g≒9kcal）
    例）唐揚げ100g → 吸油約15g（135kcal）→ 0.5人前相当 → B0.5

【揚げ物の例】
- 鶏もも唐揚げ1人前（100g）：鶏もも（160kcal, 0.5人前分）B0.5 ＋ 吸油（135kcal）B0.5 → 合計B1
- 鶏むね唐揚げ1人前（100g）：鶏むね（A食材）B0 ＋ 吸油（135kcal）B0.5 → 合計B0.5

━━━━━━━━━━━━━━━━━━━━━━━
■ ドリンクのルール
━━━━━━━━━━━━━━━━━━━━━━━
ジュース・乳飲料・炭酸飲料などは500mlを1人前として判定する。
- 500mlあたり120kcal未満 → A食材 → B0
- 500mlあたり120kcal以上 → B食材 → 量で判定

約100ml程度の摂取（0.5人前未満）はノーカウント。
プロテインドリンク（牛乳で割らないもの）はA食材。

  例）500mlで250kcal → 1人前 → B1
  例）330mlで130kcal → 500ml換算197kcal → B食材だが0.5人前未満 → B0.5

必ず以下のJSON形式のみで回答してください。JSONの前後に説明文やコードブロックは不要です：
{
  "foods": [
    {
      "name": "食材名（具体的に）",
      "category": "A",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量（例：1人前、0.5人前未満、少量トッピングなど）",
      "b_count": 0,
      "reason": "A食材のため（野菜など）"
    },
    {
      "name": "食材名（具体的に）",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量（例：1人前、0.5人前未満など）",
      "b_count": 0,
      "reason": "B食材だが0.5人前未満のためノーカウント（写真では約○g、1人前100gの○割程度）"
    },
    {
      "name": "食材名（具体的に）",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "約0.5人前",
      "b_count": 0.5,
      "reason": "B食材、写真の量は約0.5人前のためB0.5"
    },
    {
      "name": "食材名（具体的に）",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "1人前",
      "b_count": 1,
      "reason": "B食材、1人前相当のためB1"
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
    base64_image = base64.standard_b64encode(image_data).decode("utf-8")

    media_type = file.content_type
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media_type = "image/jpeg"

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


if __name__ == "__main__":
    app.run(debug=True, port=5001)
