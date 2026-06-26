import os
import io
import base64
import json
import datetime
import threading
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response
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
# ※ 512pxでは「エクレアとアンパン」のような見た目が似た食品の細部が潰れ
#   誤認識が増えるため、1024pxへ引き上げて認識精度を優先する。
MAX_IMAGE_DIMENSION = 1024
JPEG_QUALITY = 85


def repair_json(text: str) -> str:
    """トークン上限で切れたJSONを補修して返す。修復不能なら元のテキストを返す。"""
    # コードブロック除去
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    # 末尾の不完全な food エントリを削除し、配列・オブジェクトを閉じる
    try:
        # まず foods 配列の最後の完全な } を探す
        last_close = text.rfind("},\n")
        if last_close == -1:
            last_close = text.rfind("}")
        if last_close != -1:
            text = text[:last_close + 1]
        # 閉じられていない文字列・配列・オブジェクトを補完
        open_brackets = text.count("[") - text.count("]")
        open_braces   = text.count("{") - text.count("}")
        # foods 配列を閉じ、total_b_count と advice を付加
        if open_brackets > 0:
            text += "]" * open_brackets
        if open_braces > 0:
            text += "}" * open_braces
        # total_b_count / advice がなければ付加
        parsed = json.loads(text)
        if "total_b_count" not in parsed:
            parsed["total_b_count"] = sum(
                f.get("b_count", 0) for f in parsed.get("foods", [])
            )
        if "advice" not in parsed:
            parsed["advice"] = "（分析データが多いため一部省略されました）"
        return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        return text


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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_weight (
                    id         SERIAL PRIMARY KEY,
                    user_id    TEXT   NOT NULL,
                    date       TEXT   NOT NULL,
                    weight     REAL   NOT NULL,
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_weight (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT    NOT NULL,
                    date       TEXT    NOT NULL,
                    weight     REAL    NOT NULL,
                    created_at TEXT    NOT NULL,
                    UNIQUE(user_id, date)
                )
            """)
        conn.commit()
    finally:
        conn.close()


init_db()

# ── 管理者認証 ──────────────────────────────────────────────
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "abDiet2024admin")

# 食事分析1回あたりの概算コスト（円）。通常解析はHaiku 4.5（入力$1/出力$5）。
# 画像1024px・出力約1000トークンで約$0.012≒¥1.8。再計算(Sonnet)は別途だが頻度低。
# 平均的な¥2をデフォルトとする。為替やモデルを変えたら COST_PER_ANALYSIS_JPY で調整。
COST_PER_ANALYSIS_JPY = float(os.environ.get("COST_PER_ANALYSIS_JPY", "2"))

def _admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.password != ADMIN_PASSWORD:
            return Response(
                "管理者認証が必要です",
                401,
                {"WWW-Authenticate": 'Basic realm="AB Diet Admin"'},
            )
        return f(*args, **kwargs)
    return decorated

ANALYSIS_PROMPT = """この写真に写っている食事・食材をすべて特定し、以下のルールに基づいてBカウントを計算してください。

━━━━━━━━━━━━━━━━━━━━━━━
■ 商品パッケージ・お菓子の判定方法【最優先】
━━━━━━━━━━━━━━━━━━━━━━━
市販のお菓子・スナック・パッケージ商品が写っている場合は、次の手順で判定する：

【手順1】まず商品名を読み、その食材が「A食材」か「B食材」かを判断する。
【手順2】カロリーの記載がある場合、B食材については以下の基準でカウントする：
   (a) 120〜200kcal → B 0.5カウント
   (b) 200kcal以上   → B 1カウント
   ※ A食材はどれだけでも0カウント。

補足：
1. パッケージに記載された栄養成分表示が写真から読み取れる場合は、その実際のkcal値を最優先で使う
2. 読み取れない場合も、商品名から実際のkcal（一般的に知られている値）が分かるものは、その知識を使って判定する
3. 「1袋（パッケージ全体）」と「1食分（栄養成分表示の基準）」の違いに注意すること
   - 栄養成分表示が「6枚あたり」「1食分」など1袋全体ではない単位の場合は、1袋全体の合計kcalを計算して使う
   - 例：オレオ ビッツサンドバニラクリーム（6枚=87kcalと表示されているが1袋全体は200kcal超）→ 1袋食べたなら200kcal超 → B1
4. kcalも商品名もまったく特定できない場合のみ、見た目から推定したkcalで判定する

【絶対ルール】食材カテゴリは「A食材」と「B食材」の2種類のみ。「Good B」「グッドB」「Good B食材」は存在しない。必ずAかBに分類すること。

━━━━━━━━━━━━━━━━━━━━━━━
■ STEP1：食材の分類
━━━━━━━━━━━━━━━━━━━━━━━

【肉・魚・豆・乳製品の分類ルール（100gあたりのカロリー密度で判定）】
- B食材：100gあたり200kcalを超える食材
- A食材：100gあたり200kcal以下の食材
  例）鯛は100gあたり約120kcal → A食材

肉・魚・豆・乳製品のA食材例（100gあたり200kcal以下）：
- 鶏むね肉（皮なし）・ささみ（100gあたり約100〜108kcal）← A食材
- 鶏レバー・ハツ（100gあたり約120〜145kcal）← A食材
- 豚もも・牛もも・牛ヒレ（100gあたり約128〜176kcal）← A食材
- 白身魚全般・えび・いか・たこ・ホタテ・貝類（100gあたり約70〜100kcal）← A食材
- マグロ赤身・鯛・鯵・鰹（100gあたり約100〜120kcal）← A食材
- 鯖缶（水煮）（100gあたり約174kcal）← A食材
- 豆腐・こんにゃく（100gあたり約56〜70kcal）← A食材
- 納豆（100gあたり約200kcal）← A食材（200kcal以下）
- 牛乳（100gあたり約67kcal）← A食材

肉・魚・豆・乳製品のB食材例（100gあたり200kcal超）：
- 鶏もも肉（皮あり）（100gあたり約204kcal）← B食材
- 豚バラ・牛バラ・牛カルビ（100gあたり約250〜400kcal）← B食材
- ひき肉（合挽き・豚・牛）（100gあたり約200〜250kcal）← B食材
- 鮭・サーモン（100gあたり約218kcal）← B食材
- ブリ（100gあたり約257kcal）← B食材
- サバ（100gあたり約247kcal）← B食材
- サンマ・ウナギ（100gあたり約255〜318kcal）← B食材
- ツナ缶（油漬け）（100gあたり約267kcal）← B食材
- チーズ各種（100gあたり約300〜400kcal）← B食材

【肉・魚・豆・乳製品以外の分類ルール（1人前の標準カロリーで判定）】
- A食材：1人前の標準カロリーが120kcal未満 → Bカウント0
- B食材：1人前の標準カロリーが120kcal以上 → 実際のkcalでSTEP2を適用

A食材例：
- 野菜全般（生・加熱・炒め・茹でいずれも）、きのこ類、海藻類
- 果物（1人前が120kcal未満のもの）
- お茶・ブラックコーヒー・水
- 調味料少量（塩・醤油・酢・ハーブ・スパイスなど）
- プロテインパウダー単体（牛乳で割らないもの）
- 上記A食材のみで作った手作りスムージー

B食材例：
- 白米・玄米・チャーハン（1杯150g≒250kcal）
- パン全般（食パン・全粒粉パンなど）
- 麺類（そば・うどん・ラーメン・パスタなど、1人前≒270〜350kcal）
- 芋類（さつまいも・じゃがいもなど、100g≒100〜130kcal）
- バナナ（1本約80〜100kcal → 2本以上で120kcal超）
- オートミール（30〜40g≒120〜150kcal）
- スイーツ・お菓子・チョコレート（1人前120kcal以上のもの）
- 清涼飲料水・ジュース・アルコール（1人前120kcal以上）
- 油・バター・ラード（まとまった量）
- ベシャメルソース・クリームソース（1人前150g≒200kcal）

━━━━━━━━━━━━━━━━━━━━━━━
■ STEP1.5：量の見積もり【最重要・誤差を減らす】
━━━━━━━━━━━━━━━━━━━━━━━
Bカウントは「実際に食べた量」で決まる。料理名や食材から安易に「標準1人前」を当てはめると
少量の食材を大きく過大評価してしまうため、必ず写真に写っている量だけで判断すること。

【量を推定する手順】
1. 「1人前」「標準量」を初期値にしない。写真に写っている実際の量を観察してグラム数を見積もる。
2. 写真内の基準物とサイズを比較して実重量を推定する：
   - 箸の幅 ≒ 5mm、一般的な皿の直径 ≒ 20〜26cm、茶碗の直径 ≒ 11〜12cm
   - ティースプーン ≒ 5ml、大さじ ≒ 15ml、500mlペットボトルの高さ ≒ 21cm
3. 薄切り・少量の食材は特に過大評価しやすい。「枚数 × 1枚あたりの重量」で計算する：
   - ベーコン薄切り1枚 ≒ 8〜10g（約16〜20kcal）。例）1枚だけなら約10g・約18kcal。
   - ハム1枚 ≒ 10g、ロースハム薄切り1枚 ≒ 約20kcal
   - スライスチーズ1枚 ≒ 18g（約60kcal）、ベビーチーズ1個 ≒ 15g
   - 薬味・ねぎ・ごま・少量のソースなどは「ごく少量」として扱う
4. 各食材について必ず「推定グラム数 → 推定kcal」を明示し、その実kcalでSTEP2を適用する。
   amount欄とreason欄に推定グラム数と推定kcalを必ず書くこと。
5. 量が少ないときは遠慮なく少量と判定してよい（0カウント・0.5カウント・A食材化を恐れない）。

【ベーコンの量別の判定例】
- ベーコン1枚（約10g・約18kcal）→ 120kcal未満 → 0カウント
- ベーコン3枚（約30g・約55kcal）→ 120kcal未満 → 0カウント
- ベーコン1人前（約60g・約110kcal）→ 120kcal未満 → 0カウント
- ベーコン大量（約120g・約220kcal）→ 200kcal超 → 1カウント
※ベーコンは100gあたり約185kcal（A食材寄り）だが脂質が多いため、写真の実量で必ず判定する。

━━━━━━━━━━━━━━━━━━━━━━━
■ STEP2：B食材のBカウント計算【最重要】
━━━━━━━━━━━━━━━━━━━━━━━

料理名でカウントせず、食材ごとに分解して個別に判定する。
写真に写っている実際の量のカロリーを推定し、以下で判定する。
（カウントは 0 / 0.5 / 1 のみ。B食材1品につき最大1カウント）

- 実際のカロリーが120kcal未満    → 0カウント（ノーカウント）
- 実際のカロリーが120〜200kcal   → 0.5カウント
- 実際のカロリーが200kcalを超える → 1カウント

━━━━━━━━━━━━━━━━━━━━━━━
■ 具体例
━━━━━━━━━━━━━━━━━━━━━━━

【白米】
・少量（50g、約83kcal）→ 120kcal未満 → 0カウント
・半膳（75g、約125kcal）→ 120〜200kcal → 0.5カウント
・1杯（150g、約250kcal）→ 200kcal超 → 1カウント
・大盛り（200g以上、約333kcal）→ 200kcal超 → 1カウント

【コカ・コーラ（B食材：100mlあたり約43kcal）】
・小サイズ（250ml、約107kcal）→ 120kcal未満 → 0カウント
・350ml（約150kcal）→ 120〜200kcal → 0.5カウント
・ペットボトル（500ml、約215kcal）→ 200kcal超 → 1カウント

【鶏もも肉（皮あり）（B食材：100gあたり約204kcal → 200kcal超のためB食材）】
・少量（60g、約122kcal）→ 120〜200kcal → 0.5カウント
・1人前（100g、約204kcal）→ 200kcal超 → 1カウント

【鶏レバー・ハツ（A食材：100gあたり約120〜145kcal ≤ 200kcal → A食材）】
・どれだけ食べてもBカウント0（A食材のため）

【ブリ（B食材：100gあたり約257kcal → 200kcal超のためB食材）】
・刺身2切れ（約30g、約77kcal）→ 120kcal未満 → 0カウント
・刺身4〜5切れ（約60g、約154kcal）→ 120〜200kcal → 0.5カウント
・1人前（約100g、約257kcal）→ 200kcal超 → 1カウント

【グラタン1人前】
① マカロニ（底層にたっぷり、約250〜280kcal）→ 200kcal超 → 1カウント
② ベシャメルソース＋チーズ（合わせて約200kcal超）→ 200kcal超 → 1カウント
③ ひき肉（少量、約80kcal）→ 120kcal未満 → 0カウント
④ 野菜（玉ねぎなど）→ A食材 → 0カウント
→ 合計 B2

【ハンバーグ定食】
① 白米1杯（150g≒250kcal）→ 200kcal超 → 1カウント
② ひき肉1個（100g≒230kcal）→ 200kcal超 → 1カウント
③ 炒め油（小さじ1〜2、約40〜80kcal）→ 120kcal未満 → 0カウント
④ 野菜の付け合わせ → A食材 → 0カウント
→ 合計 B2

【ラーメン】
① 麺1人前（約270〜300kcal）→ 200kcal超 → 1カウント
② チャーシュー2枚（約50g≒110kcal）→ 120kcal未満 → 0カウント
③ スープの油（少量、約50kcal）→ 120kcal未満 → 0カウント
→ 合計 B1

━━━━━━━━━━━━━━━━━━━━━━━
■ 揚げ物のルール（主食材＋吸収油を個別カウント）
━━━━━━━━━━━━━━━━━━━━━━━
- 主食材：STEP1で分類し、実際のkcalでSTEP2を適用
- 衣（小麦粉・卵・パン粉）：常にノーカウント（量が少ないため）
- 吸収油：食材重量の吸油率から実際のkcalを計算し、STEP2を適用（油1g≒9kcal）

【吸油率の目安】
- から揚げ・竜田揚げ（衣が薄い）→ 約10〜15%
- 野菜の天ぷら・かき揚げ → 約25〜30%
- 魚介の天ぷら → 約15〜20%
- とんかつ・フライ（パン粉の厚い衣）→ 約15〜20%

複数の揚げ物は吸油量を合計してからSTEP2で判定する。

【計算例】
- 鶏もも唐揚げ1人前（100g）
  ① 鶏もも肉（B食材、100gで約204kcal）→ 200kcal超 → 1カウント
  ② 衣 → 0カウント
  ③ 吸油：100g × 15% ≒ 15g（135kcal）→ 120〜200kcal → 0.5カウント
  ✅ 合計：B1.5カウント → 各食材の最大は1なので B1（鶏もも）+ B0.5（吸油）= B1.5カウント

- 鶏むね唐揚げ1人前（100g）
  ① 鶏むね肉（A食材、100gで約105kcal）→ 0カウント
  ② 衣 → 0カウント
  ③ 吸油：約15g（135kcal）→ 120〜200kcal → 0.5カウント
  ✅ 合計：B0.5カウント

- 野菜の天ぷら3個（合計100g）
  ① 野菜 → A食材 → 0カウント
  ② 衣 → 0カウント
  ③ 吸油：100g × 28% ≒ 28g（252kcal）→ 200kcal超 → 1カウント
  ✅ 合計：B1カウント

━━━━━━━━━━━━━━━━━━━━━━━
■ ドリンクのルール
━━━━━━━━━━━━━━━━━━━━━━━
飲み物も同じルールを適用する。写真に写っているボトル・缶の実際の容量から摂取カロリーを推定し、STEP2で判定する。

- お茶・ブラックコーヒー・水 → A食材 → 0カウント
- 清涼飲料水・ジュース・アルコールなど → 実際のkcalをSTEP2で判定
  例）コカ・コーラ250ml（約107kcal）→ 120kcal未満 → 0カウント
  例）コカ・コーラ350ml（約150kcal）→ 120〜200kcal → 0.5カウント
  例）コカ・コーラ500ml（約215kcal）→ 200kcal超 → 1カウント
- 牛乳（B食材：飲料は500mlを1人前とするため1人前≒335kcal → 200kcal超のためB食材）
  実際に飲んだ量で判定：
  例）180ml（約120kcal）→ 120〜200kcal → 0.5カウント
  例）500ml（約335kcal）→ 200kcal超 → 1カウント
- プロテインドリンク（牛乳で割らないもの）→ A食材 → 0カウント

━━━━━━━━━━━━━━━━━━━━━━━
■ 油・バターのルール【油を過小評価しないこと】
━━━━━━━━━━━━━━━━━━━━━━━
油・バター・ラードもB食材として扱い、実際のkcalでSTEP2を適用する。（油1g≒9kcal）
画像では油の量が見えにくく過小評価しがちなので、調理に油が使われていると判断したら下記の基準で必ず計上する。

【油の量別の基準（油1g≒9kcal）】
- 小さじ2（約10g・約90kcal）→ 120kcal未満 → 0カウント
- 大さじ1（15g × 9 ＝ 135kcal）→ 120〜200kcal → 0.5カウント　← 基本の目安
- 大さじ2（30g ≒ 270kcal）→ 200kcal超 → 1カウント

【調理油の見積もり】
炒め物・揚げ物・チャーハン・ソテーなど油を使う料理で、油が使われている（テリ・コク・炒め調理が見て取れる）と判断した場合は、
過小評価を避けるため最低でも「大さじ1（135kcal）＝B0.5カウント」として油を1品計上する。
たっぷりの油（揚げ物・油通し・炒め油が多い）と判断したら大さじ2（270kcal）＝B1カウントとする。
- ドレッシング（大さじ1.5程度、約165kcal）→ 0.5カウント

必ず以下のJSON形式のみで回答してください。JSONの前後に説明文やコードブロックは不要です：
【重要】categoryフィールドは "A" または "B" のみ使用すること。"Good B"・"グッドB" は使用禁止。
{
  "foods": [
    {
      "name": "食材名（具体的に）",
      "category": "A",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量（例：たっぷり、1人前、少量など）",
      "b_count": 0,
      "reason": "A食材のため（1人前約○kcal、120kcal未満）Bカウント0"
    },
    {
      "name": "食材名（具体的に）",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量と推定kcal（例：1人前・約250kcal）",
      "b_count": 1,
      "reason": "B食材（1人前約○kcal）、実際の摂取量約○kcalが200kcal超のためB1"
    },
    {
      "name": "食材名（具体的に）",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量と推定kcal（例：約150kcal相当）",
      "b_count": 0.5,
      "reason": "B食材（1人前約○kcal）、実際の摂取量約○kcalが120〜200kcalのためB0.5"
    },
    {
      "name": "食材名（具体的に）",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量と推定kcal（例：少量・約70kcal）",
      "b_count": 0,
      "reason": "B食材だが実際の摂取量約○kcalが120kcal未満のためノーカウント"
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
@_admin_required
def admin():
    return render_template("admin.html")


@app.route("/api/admin/overview")
@_admin_required
def admin_overview():
    now = datetime.datetime.now(JST)
    today_start = now.strftime("%Y-%m-%dT00:00:00")
    month_start_ts = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")
    month_start_dt = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM usage_log")
        total_users = cur.fetchone()[0] or 0

        cur.execute(
            f"SELECT COUNT(DISTINCT user_id) FROM usage_log WHERE created_at >= {PH}",
            (month_start_ts,),
        )
        mau = cur.fetchone()[0] or 0

        cur.execute(
            f"SELECT COUNT(*) FROM usage_log WHERE created_at >= {PH}",
            (today_start,),
        )
        today_analyses = cur.fetchone()[0] or 0

        cur.execute(
            f"SELECT AVG(b_count) FROM daily_b_count WHERE date >= {PH}",
            (month_start_dt,),
        )
        avg_b_row = cur.fetchone()[0]
    finally:
        conn.close()

    return jsonify({
        "total_users": total_users,
        "mau": mau,
        "today_analyses": today_analyses,
        "avg_b_count": round(float(avg_b_row), 2) if avg_b_row is not None else None,
        "cost_per_analysis_jpy": COST_PER_ANALYSIS_JPY,
        "est_cost_today_jpy": round(today_analyses * COST_PER_ANALYSIS_JPY),
    })


@app.route("/api/admin/daily-trend")
@_admin_required
def admin_daily_trend():
    now = datetime.datetime.now(JST)
    start_dt = now.date() - datetime.timedelta(days=29)
    start_ts = start_dt.strftime("%Y-%m-%dT00:00:00")
    start_str = start_dt.strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT SUBSTR(created_at, 1, 10) AS dt,
                       COUNT(DISTINCT user_id) AS users, COUNT(*) AS analyses
               FROM usage_log WHERE created_at >= {PH}
               GROUP BY dt ORDER BY dt""",
            (start_ts,),
        )
        rows = cur.fetchall()
        usage_by_day    = {row[0]: row[1] for row in rows}
        analyses_by_day = {row[0]: row[2] for row in rows}

        cur.execute(
            f"""SELECT date, AVG(b_count)
               FROM daily_b_count WHERE date >= {PH}
               GROUP BY date ORDER BY date""",
            (start_str,),
        )
        b_by_day = {row[0]: round(float(row[1]), 2) for row in cur.fetchall()}
    finally:
        conn.close()

    all_dates = []
    d = start_dt
    end = now.date()
    while d <= end:
        all_dates.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)

    return jsonify({
        "dates": all_dates,
        "active_users": [usage_by_day.get(dt, 0) for dt in all_dates],
        "analyses": [analyses_by_day.get(dt, 0) for dt in all_dates],
        "est_costs_jpy": [round(analyses_by_day.get(dt, 0) * COST_PER_ANALYSIS_JPY) for dt in all_dates],
        "avg_b_counts": [b_by_day.get(dt) for dt in all_dates],
    })


@app.route("/api/admin/users")
@_admin_required
def admin_users():
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT user_id, COUNT(*) AS days, AVG(b_count) AS avg_b, MAX(date) AS last_active
               FROM daily_b_count GROUP BY user_id ORDER BY last_active DESC"""
        )
        b_stats = {
            row[0]: {
                "days": row[1],
                "avg_b": round(float(row[2]), 2) if row[2] is not None else None,
                "last_active": row[3],
            }
            for row in cur.fetchall()
        }

        cur.execute(
            "SELECT user_id, COUNT(*) FROM usage_log GROUP BY user_id"
        )
        analysis_counts = {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()

    all_ids = set(b_stats.keys()) | set(analysis_counts.keys())
    users = []
    for uid in all_ids:
        bs = b_stats.get(uid, {})
        users.append({
            "user_id_short": uid[:8] + "…",
            "days_recorded": bs.get("days", 0),
            "avg_b_count": bs.get("avg_b"),
            "analyses_count": analysis_counts.get(uid, 0),
            "last_active": bs.get("last_active", ""),
        })
    users.sort(key=lambda x: x["last_active"] or "", reverse=True)

    return jsonify({"users": users})


# ── 性別・目標に合わせたアドバイス用コンテキスト ──────────────
# 判定基準（kcal/Bカウント計算）は変えず、advice欄だけ目標に寄せる。
_GOAL_ADVICE = {
    "female": {
        "cut":      "女性・減量目標（1日の目標Bカウントは4回以内）",
        "maintain": "女性・体重維持目標（1日の目標Bカウントは5〜6回）",
        "bulk":     "女性・増量目標（1日の目標Bカウントは6回以上）",
    },
    "male": {
        "cut":      "男性・減量目標（1日の目標Bカウントは6回以内）",
        "maintain": "男性・体重維持目標（1日の目標Bカウントは7〜8回）",
        "bulk":     "男性・増量目標（1日の目標Bカウントは8回以上）",
    },
}


def _advice_context(gender, goal):
    """ユーザーの性別・目標から、advice欄をパーソナライズする指示文を返す。未設定ならNone。"""
    desc = _GOAL_ADVICE.get(gender, {}).get(goal)
    if not desc:
        return None
    if goal == "cut":
        direction = (
            "この人は減量が目標です。advice欄では、B食材（高カロリーな食材）を"
            "減らす・量を控える・A食材に置き換える方向で、具体的かつ前向きに提案してください。"
        )
    elif goal == "bulk":
        direction = (
            "この人は増量が目標です。advice欄では、B食材をしっかり摂って"
            "増やす方向で、具体的かつ前向きに提案してください。"
        )
    else:
        direction = (
            "この人は体重維持が目標です。advice欄では、今のB食材の量・バランスを"
            "保つ方向で、具体的かつ前向きに提案してください。"
        )
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "【このユーザーの目標】\n"
        f"{desc}\n"
        f"{direction}\n"
        "【重要】kcalやBカウントの判定基準は一切変更しないこと。変えるのはadvice欄の文面だけ。\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )


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


@app.route("/api/daily-weight", methods=["POST"])
def post_daily_weight():
    """今日の体重を記録（タップ入力時に呼ばれる）"""
    data = request.get_json()
    uid    = (data or {}).get("user_id", "").strip()
    weight = (data or {}).get("weight", 0)
    date   = (data or {}).get("date", "")   # YYYY-MM-DD (JST)
    try:
        weight = float(weight)
    except (TypeError, ValueError):
        weight = 0
    if not uid or not date or weight <= 0:
        return jsonify({"error": "パラメータ不足"}), 400

    ts = datetime.datetime.now(JST).isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            if USE_PG:
                cur.execute(
                    """INSERT INTO daily_weight (user_id, date, weight, created_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, date) DO UPDATE SET weight=%s, created_at=%s""",
                    (uid, date, weight, ts, weight, ts)
                )
            else:
                cur.execute(
                    """INSERT INTO daily_weight (user_id, date, weight, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(user_id, date) DO UPDATE SET weight=excluded.weight, created_at=excluded.created_at""",
                    (uid, date, weight, ts)
                )
            conn.commit()
        finally:
            conn.close()
    return jsonify({"ok": True})


@app.route("/api/weekly-b")
@app.route("/api/monthly-b")
def get_monthly_b():
    """直近30日間のBカウント履歴＋体重を返す"""
    uid = request.args.get("user_id", "")
    if not uid:
        return jsonify({"total": 0, "days_count": 0, "daily": [], "date_start": "", "date_end": ""})

    now = datetime.datetime.now(JST)
    date_end   = now.strftime("%Y-%m-%d")
    date_start = (now - datetime.timedelta(days=29)).strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT date, b_count FROM daily_b_count
               WHERE user_id={PH} AND date>={PH} AND date<={PH}
               ORDER BY date""",
            (uid, date_start, date_end)
        )
        b_rows = cur.fetchall()

        cur.execute(
            f"""SELECT date, weight FROM daily_weight
               WHERE user_id={PH} AND date>={PH} AND date<={PH}
               ORDER BY date""",
            (uid, date_start, date_end)
        )
        w_rows = cur.fetchall()
    finally:
        conn.close()

    b_by_date = {r[0]: r[1] for r in b_rows}
    w_by_date = {r[0]: r[1] for r in w_rows}

    total      = sum(b_by_date.values())
    days_count = len(b_by_date)
    all_dates  = sorted(set(b_by_date) | set(w_by_date))
    daily      = [
        {"date": d, "b_count": b_by_date.get(d), "weight": w_by_date.get(d)}
        for d in all_dates
    ]
    return jsonify({
        "total":      total,
        "days_count": days_count,
        "daily":      daily,
        "date_start": date_start,
        "date_end":   date_end,
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

    # 性別・目標に合わせたアドバイス指示（設定済みのときのみ付与）
    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64_image,
            },
        },
    ]
    adv = _advice_context(request.form.get("gender", ""), request.form.get("goal", ""))
    if adv:
        user_content.append({"type": "text", "text": adv})

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",   # 通常解析はコスト重視でHaiku
            max_tokens=2500,
            system=[
                {
                    "type": "text",
                    "text": ANALYSIS_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": user_content,
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

        try:
            result = json.loads(result_text)
        except json.JSONDecodeError:
            result = json.loads(repair_json(result_text))
        return jsonify(result)

    except json.JSONDecodeError as e:
        return jsonify({"error": f"分析結果の解析に失敗しました。写真をもう一度撮り直してお試しください。({e})"}), 500
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

    correction_text = f"""━━━━━━━━━━━━━━━━━━━━━━━
【ユーザーからの補足・訂正情報】
{correction}

この補足情報を最優先して、ABダイエットルールに基づき再計算してください。
━━━━━━━━━━━━━━━━━━━━━━━"""

    reanalyze_content = [
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
            "text": correction_text,
        },
    ]
    adv = _advice_context(request.form.get("gender", ""), request.form.get("goal", ""))
    if adv:
        reanalyze_content.append({"type": "text", "text": adv})

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",   # 再計算（修正時）は精度重視でSonnet
            max_tokens=2500,
            system=[
                {
                    "type": "text",
                    "text": ANALYSIS_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": reanalyze_content,
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


@app.route("/analyze-text", methods=["POST"])
def analyze_text():
    """写真を撮り忘れたとき用：食事内容を文章で受け取り、Bカウントを推定する。"""
    client = get_client()
    if client is None:
        return jsonify({"error": "APIキーが設定されていません。設定画面から登録してください。"}), 401

    text = (request.form.get("text", "") or "").strip()
    if not text:
        return jsonify({"error": "食べた内容を入力してください"}), 400

    # 利用ログ記録（写真分析と同じく1回としてカウント）
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

    text_prompt = f"""━━━━━━━━━━━━━━━━━━━━━━━
【テキスト入力モード・写真なし】
ユーザーは食事の写真を撮り忘れたため、食べた内容を文章で入力しました。
以下の説明から食材と量を推定し、システムプロンプトのABダイエットのルールに従ってBカウントを判定してください。
量が明記されていない場合は、一般的な1人前として推定してください。

【食べたもの（ユーザー入力）】
{text}

写真がないため推定が大まかになる旨を、advice欄の最後に一言添えてください。
出力は通常のJSON（foods / total_b_count / advice）のみで構いません。
━━━━━━━━━━━━━━━━━━━━━━━"""

    text_content = [{"type": "text", "text": text_prompt}]
    adv = _advice_context(request.form.get("gender", ""), request.form.get("goal", ""))
    if adv:
        text_content.append({"type": "text", "text": adv})

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",   # 文章入力もコスト重視でHaiku
            max_tokens=2500,
            system=[
                {
                    "type": "text",
                    "text": ANALYSIS_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": text_content}],
        )

        result_text = response.content[0].text.strip()
        if result_text.startswith("```"):
            lines = result_text.split("\n")[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            result_text = "\n".join(lines)

        try:
            result = json.loads(result_text)
        except json.JSONDecodeError:
            result = json.loads(repair_json(result_text))
        return jsonify(result)

    except json.JSONDecodeError as e:
        return jsonify({"error": f"分析結果の解析に失敗しました。もう一度お試しください。({e})"}), 500
    except anthropic.AuthenticationError:
        return jsonify({"error": "APIキーが無効です。設定画面で正しいキーを入力してください。"}), 401
    except anthropic.APIError as e:
        return jsonify({"error": f"AI分析中にエラーが発生しました: {e}"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
