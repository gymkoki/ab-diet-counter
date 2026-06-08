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

【ABダイエット ルール】

━━━━━━━━━━━━━━━━━━━━━━━
■ STEP1：食材カテゴリの判定
━━━━━━━━━━━━━━━━━━━━━━━

【A食材】― どれだけ食べてもBカウント0
以下に該当する食材は量に関係なく常にA食材。
- 野菜全般（生・加熱・炒め・茹でいずれも）
- 果物全般
- きのこ類・海藻類
- 豆腐・こんにゃく・大豆製品（納豆を除く）
- プロテインパウダー単体
- 調味料少量（黒胡椒・塩・醤油・酢・ハーブ・スパイスなど）
- お茶・ブラックコーヒー・水
- 手作りスムージー（上記A食材の素材のみ使用）
※ 肉・魚・豆・乳製品は下記「タンパク質食材の分類ルール」で判定する

【B食材】― 1人前あたり120kcal以上の食材（量によってB0〜B2カウント）
- 白米（1杯150g、約250kcal）
- パン（食パン2枚、約250kcal）
- 麺類（1人前、約270〜350kcal）
- 揚げ物（吸油込みで計算、後述）
- 油・バター（大さじ1.5以上、約135kcal超）
- スイーツ・お菓子（1人前で120kcal超のもの）
- アルコール飲料（1人前で120kcal超のもの）
- 牛乳（1人前400ml、約250kcal）
- 納豆（1人前2パック、約140kcal）
- バナナ（1〜2本、約80〜200kcal）
- オートミール（1人前30〜40g、約120〜150kcal）
- トウモロコシ（1本約150kcal）
- さつまいも（1人前100g、約130kcal）
- 全粒粉パン1枚（約120〜140kcal）
※ 肉・魚でB食材に該当するものは下記「タンパク質食材の分類ルール」で判定する

━━━━━━━━━━━━━━━━━━━━━━━
■ タンパク質食材の分類ルール（肉・魚・豆・乳製品）
━━━━━━━━━━━━━━━━━━━━━━━

肉・魚・豆類・乳製品は「1人前＝100g」を基準に、100gあたりのカロリーでカテゴリを決める。
実際に食べた量のカロリーをSTEP2のしきい値でカウントする。

【A食材：100gあたり120kcal未満】― 量に関係なくBカウント0
- 鶏むね肉（皮なし）・ささみ：約98〜108kcal
- マグロ赤身：約115kcal
- タラ・ヒラメ・カレイ・アジなどの白身・低脂質魚：約70〜100kcal
- えび・いか・たこ・ホタテ・貝類：約80〜100kcal
- 木綿豆腐・絹豆腐：約56〜70kcal

【B食材：100gあたり120kcal以上】― 実際のkcalで0〜2カウント
- 皮付き鶏もも肉：約160kcal
- 豚もも肉（赤身）：約128kcal
- 牛もも肉（赤身）：約140kcal
- ツナ缶（油漬け、汁を切った状態）：約130kcal
- 鯖缶（水煮）：約130kcal
- 鮭・サーモン（生・焼き・ムニエルなど）：約200〜220kcal
- ブリ（生・焼き）：約257kcal
- サバ（生・焼き）：約247kcal
- サンマ（生・焼き）：約318kcal
- ウナギ（蒲焼き）：約255kcal
- 豚バラ肉：約386kcal
- 牛バラ・サーロイン・リブロース：約200kcal以上
- チーズ（1人前30g以上で約120kcal超）

【カウント例：豚もも肉（B食材、1人前100g≒128kcal）】
- 約30〜40g（約38〜51kcal）→ 120kcal以下 → B0カウント（半人前未満）
- 約100g（約128kcal）→ 121〜199kcal → B0.5カウント（1人前）
- 約160g（約205kcal）→ 200kcal以上 → B1カウント（1.5人前超）

【カウント例：鮭（B食材、1人前100g≒210kcal）】
- 約30g（約63kcal）→ 120kcal以下 → B0カウント
- 約80g（約168kcal）→ 121〜199kcal → B0.5カウント
- 約100g（約210kcal）→ 200kcal以上 → B1カウント
- 約200g以上（約420kcal以上）→ B2カウント（2人前超）

━━━━━━━━━━━━━━━━━━━━━━━
■ Bカウントの基本原則【最重要・必ず守ること】
━━━━━━━━━━━━━━━━━━━━━━━

【原則1：料理名ではなく「B食材の数」で判定する】
「ハンバーグ定食」「ラーメン」など料理単位でカウントしない。
料理に含まれるB食材をそれぞれ独立して分解し、個別にカウントする。

【原則2：Bカウントは実際に食べた量のkcalで決まる】
- 食べた量が200kcal以上（1人前相当）→ B1
- 食べた量が121〜199kcal（半人前相当）→ B0.5
- 食べた量が120kcal以下（半人前未満・少量・トッピング）→ B0

【原則3：具材・トッピング・少量添加のB食材はB0】
B食材でも「少し入っている程度」「トッピング」「料理に使う調理油が小さじ2程度」
など半人前未満の量はB0でよい。主役として一人前食べているときのみカウントする。

【ハンバーグ定食の具体例】
① 白米（1杯150g、約250kcal） → B食材1人前 → B1
② ひき肉（ハンバーグ1個、約100g、約250kcal） → B食材1人前 → B1
③ 炒め油（小さじ2程度、約80kcal） → B食材だが半人前未満 → B0
④ 野菜の付け合わせ → A食材 → B0
⑤ ソース・調味料少量 → A食材扱い → B0
→ 合計 B2

【ラーメンの具体例】
① 麺（1人前、約270kcal） → B食材1人前 → B1
② チャーシュー（1〜2枚、約50g、約130kcal） → B食材だがトッピング程度 → B0.5
③ スープの油・ラード少量 → B食材だが少量 → B0
→ 合計 B1〜B1.5

【焼肉定食の具体例】
① 白米（1杯） → B1
② 牛バラ肉（1人前100g、約317kcal） → B1
③ 焼き油（少量） → B0
→ 合計 B2

━━━━━━━━━━━━━━━━━━━━━━━
■ STEP2：量によるBカウント計算
━━━━━━━━━━━━━━━━━━━━━━━

【A食材のカウント】
→ 量に関係なく常に0カウント。大量に食べても0。

【B食材のカウント】
写真に写っている実際の量のカロリーを推定して判定：
- 実際のカロリーが120kcal以下（少量・半人前未満・トッピング程度）→ 0カウント
- 実際のカロリーが121〜199kcal（半人前相当）→ 0.5カウント
- 実際のカロリーが200kcal以上（1人前）→ 1カウント
- 実際のカロリーが400kcal以上（超大盛り・2人前以上）→ 2カウント

「大盛り」は必ずしもB2ではない。B2になるのは「2人前に近い超大盛り」の場合のみ。

  例）白米（1人前150g≒250kcal）
  ・少量（30g、約50kcal）→ 120kcal以下 → B0カウント
  ・半膳（75g、約125kcal）→ 121〜199kcal → B0.5カウント
  ・1杯（150g、約250kcal）→ 200kcal以上 → B1カウント
  ・普通の大盛り（200g、約333kcal）→ B1カウント（B2にはならない）
  ・超大盛り（300g以上、約500kcal以上）→ B2カウント

  例）豚もも肉（B食材、1人前100g≒128kcal）
  ・少量（30〜40g、約38〜51kcal）→ 120kcal以下 → B0カウント（半人前未満）
  ・1人前（100g、約128kcal）→ 121〜199kcal → B0.5カウント
  ・約160g（約205kcal）→ 200kcal以上 → B1カウント

  例）鮭（B食材、1人前100g≒210kcal）
  ・少量（約30g、約63kcal）→ 120kcal以下 → B0カウント
  ・約80g（約168kcal）→ 121〜199kcal → B0.5カウント
  ・1人前（約100g、約210kcal）→ 200kcal以上 → B1カウント

  例）油・ドレッシング（1人前＝大さじ2≒200kcal）
  ・小さじ1（約40kcal）→ 120kcal以下 → B0カウント
  ・大さじ1（約110kcal）→ 120kcal以下 → B0カウント
  ・大さじ1〜1.5程度（約121〜165kcal）→ 121〜199kcal → B0.5カウント
  ・大さじ2以上（約200kcal以上）→ B1カウント

  例）納豆（B食材）
  ・1パック（70kcal）→ 120kcal以下 → B0カウント
  ・2パック（140kcal）→ 121〜199kcal → B0.5カウント
  ・3パック（210kcal）→ 200kcal以上 → B1カウント

━━━━━━━━━━━━━━━━━━━━━━━
■ 揚げ物の特別ルール（食材を分解してそれぞれカウント）
━━━━━━━━━━━━━━━━━━━━━━━
揚げ物・天ぷら・フライ類は、以下の3要素に分解してそれぞれ独立にカウントする。

【要素1：主食材（肉・魚・野菜など）】
→ タンパク質食材の分類ルールに従い、実際のkcalでSTEP2のしきい値を適用する
  - 野菜・きのこ → A食材 → 0カウント
  - 鶏むね肉（皮なし）→ A食材（100gあたり約108kcal）→ 0カウント
  - 鶏もも肉（皮付き）→ B食材（100gあたり約160kcal）→ 実際のkcalで判定（100g=160kcal→B0.5）

【要素2：衣（小麦粉・卵・パン粉）】
→ 1人前の衣量は少量のため、常に0カウント

【要素3：吸収した油】
→ 揚げ物は食材重量の約20%の油を吸収する。その吸収油のカロリーで以下のルールを適用する：
  - 吸収油が120kcal未満 → 0カウント
  - 吸収油が121〜199kcal → 0.5カウント
  - 吸収油が200kcal以上 → 1カウント

【吸収油カロリーの目安（油1g≒9kcal）】
  - 薄衣の唐揚げ 100g → 吸油約15g（135kcal）→ B0.5カウント
  - 厚衣のフライ・フライドチキン 100g → 吸油約20〜25g（180〜225kcal）→ B0.5〜B1カウント
  - 大量の揚げ物（200g超）→ 吸油が200kcal超えることが多い → B1カウント

【具体例】
- 鶏もも肉（皮付き）唐揚げ 1人前（100g）
  1. 鶏もも肉（皮付き）= B食材（100g≒160kcal）→ 121〜199kcal → B0.5カウント
  2. 衣（小麦粉・卵）= 少量 → 0カウント
  3. 吸油 約15g（135kcal）→ 121〜199kcal → B0.5カウント
  ✅ 合計：B1カウント

- 鶏むね肉（皮なし）唐揚げ 1人前（100g）
  1. 鶏むね肉（皮なし）= A食材 → 0カウント
  2. 衣 = 少量 → 0カウント
  3. 吸油 約15g（135kcal）→ 121〜199kcal → B0.5カウント
  ✅ 合計：B0.5カウント

- フライドチキン（皮付き・厚衣）1人前（100g）
  1. 鶏もも肉（皮付き）= B食材（100g≒160kcal）→ B0.5カウント
  2. 衣 = 少量 → 0カウント
  3. 吸油 約22g（200kcal）→ 200kcal以上 → B1カウント
  ✅ 合計：B1.5カウント

- 野菜の天ぷら 2〜3個（合計80g）
  1. 野菜 = A食材 → 0カウント
  2. 衣 = 少量 → 0カウント
  3. 吸油 約16g（144kcal）→ 121〜199kcal → B0.5カウント
  ✅ 合計：B0.5カウント

- 野菜の素揚げ（少量・40g程度）
  1. 野菜 = A食材 → 0カウント
  2. 吸油 約8g（72kcal）→ 120kcal以下 → B0カウント
  ✅ 合計：B0カウント

━━━━━━━━━━━━━━━━━━━━━━━
■ 市販ドリンクのルール（500ml換算）
━━━━━━━━━━━━━━━━━━━━━━━
市販の飲み物（ジュース・乳飲料・炭酸飲料・スポーツドリンクなど）は
実際の容量を500ml換算してカテゴリを判定する。
- 500ml換算で120kcal未満 → A食材 → B0カウント
- 500ml換算で121〜199kcal → B食材 → B0.5カウント
- 500ml換算で200kcal以上 → B食材 → B1カウント

  例）330mlで130kcal → 500ml換算197kcal → B0.5カウント
  例）500mlで250kcal → B1カウント

牛乳のみ例外：1人前＝400ml（約250kcal）→ B食材 → 量に比例してカウント

必ず以下のJSON形式のみで回答してください。JSONの前後に説明文やコードブロックは不要です：
{
  "foods": [
    {
      "name": "食材・料理名",
      "category": "A",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量の説明（例：1人前、半人前、大盛りなど）",
      "b_count": 0,
      "reason": "A食材のため（1人前約○kcal、120kcal未満）"
    },
    {
      "name": "食材・料理名",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量の説明",
      "b_count": 0,
      "reason": "B食材（1人前約○kcal）だが半人前未満（実際約○kcal）のためBカウント0"
    },
    {
      "name": "食材・料理名",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量の説明",
      "b_count": 0.5,
      "reason": "B食材（1人前約○kcal）、実際の量約○kcal（121〜199kcal）のためBカウント0.5"
    },
    {
      "name": "食材・料理名",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量の説明",
      "b_count": 1,
      "reason": "B食材（1人前約○kcal）、実際の量約○kcal（200kcal以上）のためBカウント1"
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
