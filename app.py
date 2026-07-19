import os
import io
import gc
import re
import gzip
import base64
import json
import time
import secrets
import datetime
import threading
import traceback
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.header import Header
from email import encoders
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

# アップロード上限（メモリ保護）。巨大ファイルはここで413にして受け付けない。
# フロントは画像を圧縮して送るため通常は1MB未満。16MBは十分な余裕。
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

# ── 起動高速化 ──
# index.html は1ファイル約300KBあり、無圧縮のままだとスマホ回線で
# ダウンロードに数秒かかり「開いてから数秒真っ白」の原因になる。
# gzip/brotli圧縮で約1/5に縮めて配信する（未インストール環境でも起動は継続）。
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass
# 静的ファイル（キャラ画像・アイコン等）は30日キャッシュ。
# 更新時はURLの ?v= を変えて配信し直す運用のため長めでも安全。
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60 * 60 * 24 * 30


@app.errorhandler(Exception)
def _handle_uncaught_exception(e):
    """想定外の例外でも、HTMLの500ではなく必ずJSONの{error}を返す。
    フロントが `res.json()` でメッセージを読めるようにし、原因はログに残して後から追える。
    （例：解析前のDB書き込みや外部APIが一時的に落ちても、汎用500で終わらせない）"""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e  # 404・405・413等はFlask標準の扱いのまま
    app.logger.error("Unhandled exception:\n%s", traceback.format_exc())
    return jsonify({
        "error": "サーバー側で一時的なエラーが発生しました。少し時間をおいて、もう一度お試しください。"
    }), 500

# ── 画像の縮小・圧縮（AI分析の高速化＋メモリ保護）──────────
# Claude Visionは画像サイズが大きいほど処理（トークン化）に時間がかかるため、
# 分析前に長辺を最大1024pxへ縮小し、JPEGで圧縮してから送信する。
# ※ 512pxでは「エクレアとアンパン」のような見た目が似た食品の細部が潰れ
#   誤認識が増えるため、1024pxへ引き上げて認識精度を優先する。
MAX_IMAGE_DIMENSION = 1024
JPEG_QUALITY = 85

# 復号時のメモリ暴走（画像爆弾）対策：この画素数を超える画像は展開しない。
if Image is not None:
    Image.MAX_IMAGE_PIXELS = 50_000_000  # 5000万画素（一般的なスマホ写真は十分収まる）


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
        if "total_protein_g" not in parsed:
            parsed["total_protein_g"] = sum(
                f.get("protein_g", 0) or 0 for f in parsed.get("foods", [])
            )
        if "total_veg_g" not in parsed:
            parsed["total_veg_g"] = sum(
                f.get("veg_g", 0) or 0 for f in parsed.get("foods", [])
            )
        if "total_fruit_g" not in parsed:
            parsed["total_fruit_g"] = sum(
                f.get("fruit_g", 0) or 0 for f in parsed.get("foods", [])
            )
        if "advice" not in parsed:
            parsed["advice"] = "（分析データが多いため一部省略されました）"
        return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        return text


class EmptyAIResponse(Exception):
    """AIの応答本文が空だったことを表す。detailに切り分け用の情報を持つ。"""
    def __init__(self, detail=""):
        super().__init__(detail)
        self.detail = detail


def parse_ai_result(response):
    """Claudeの応答から本文テキストを取り出してJSON(dict)にする。
    ・複数のテキストブロックを連結（先頭が空ブロックでも取りこぼさない）
    ・コードフェンス(```～)を除去
    ・本文が空なら EmptyAIResponse を送出
    ・JSONが途中で切れている等はrepair_jsonで補修してから読む"""
    blocks = getattr(response, "content", []) or []
    text = "".join(
        getattr(b, "text", "") for b in blocks
        if getattr(b, "type", None) == "text"
    ).strip()
    if not text:
        stop = getattr(response, "stop_reason", "?")
        types = ",".join(getattr(b, "type", "?") for b in blocks) or "none"
        raise EmptyAIResponse(f"stop={stop};blocks={types}")

    # AIが前置き文やコードフェンス(```json ～ ```)を付けてくることがあるため、
    # 本文のどこにあってもJSON本体だけを取り出す（先頭が{で始まらなくてもOK）。
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    if not text.startswith("{"):
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            text = text[s:e + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(repair_json(text))


def _num(v):
    """値を数値化。None・空文字・非数値は0として扱う。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def recompute_totals(result):
    """total_* を必ず foods の合計に一致させる。
    AIが返す合計値は内訳（各foodのb_count等）とまれに食い違うことがあるため、
    表示・日次集計の基準になる合計は常にサーバー側で内訳から再計算して整合させる。
    （例：白米B1＋温玉B0.5＋油B0.5 の内訳なのに total_b_count が3で返る、を防ぐ）"""
    if not isinstance(result, dict):
        return result
    foods = result.get("foods")
    if isinstance(foods, list):
        # Bカウントは0.5刻み。浮動小数の誤差を除くため0.5単位に丸める。
        b_sum = sum(_num(f.get("b_count")) for f in foods)
        result["total_b_count"] = round(b_sum * 2) / 2
        result["total_protein_g"] = round(sum(_num(f.get("protein_g")) for f in foods))
        result["total_veg_g"] = round(sum(_num(f.get("veg_g")) for f in foods))
        result["total_fruit_g"] = round(sum(_num(f.get("fruit_g")) for f in foods))
    return result


def create_and_parse(client, **kwargs):
    """messages.create を呼び、失敗しても数回まで自動リトライしてからJSONを返す。
    ・空応答／JSONが壊れていた場合は生成し直す（AIの一時的なブレ対策）
    ・レート制限・過負荷(529)・一時的なサーバーエラーは少し待って再試行
    返却前に total_* を内訳(foods)の合計へ揃え、合計と明細の食い違いを防ぐ。"""
    detail = ""
    last_json_err = None
    # 最大2回まで（1回失敗したらもう1回だけ生成し直す）。
    # クライアントtimeout=140秒 × 2回 ＝ 最大280秒で、gunicornの --timeout=300秒を
    # 超えない。これでワーカーが強制終了されて500/502になるのを防ぐ。
    attempts = 2
    for attempt in range(attempts):
        try:
            response = client.messages.create(**kwargs)
            result = recompute_totals(parse_ai_result(response))
            # foods が無い等、想定外の形なら生成し直す
            if not isinstance(result, dict) or "foods" not in result:
                raise EmptyAIResponse("no-foods")
            return result
        except EmptyAIResponse as e:
            detail = e.detail
        except json.JSONDecodeError as e:
            last_json_err = e   # JSONが壊れていた → もう一度生成し直す
        except (anthropic.RateLimitError, anthropic.InternalServerError,
                anthropic.APITimeoutError, anthropic.APIConnectionError):
            pass  # 一時的なAPIエラーは待って再試行
        except anthropic.APIStatusError as e:
            # 529(過負荷) や 5xx など一時的なものだけ再試行。その他は即失敗させる。
            if getattr(e, "status_code", None) not in (429, 500, 502, 503, 529):
                raise
        if attempt < attempts - 1:
            time.sleep(0.7)  # 軽くバックオフしてから1回だけ再試行
    # リトライしても駄目だった場合は、呼び出し側でエラー表示できるよう送出する
    if last_json_err is not None:
        raise last_json_err
    raise EmptyAIResponse(detail or "retry-exhausted")


def prepare_image_for_api(image_data: bytes, fallback_media_type: str):
    """画像を縮小・圧縮し、(base64文字列, media_type) を返す。
    大きな写真でもメモリを使いすぎないよう、JPEGは復号時点で目標サイズ付近まで
    間引き（draft）、処理後は画像・バッファを明示的に解放する。"""
    if Image is None:
        return base64.standard_b64encode(image_data).decode("utf-8"), fallback_media_type

    src = img = None
    try:
        src = Image.open(io.BytesIO(image_data))
        # JPEGは復号する時点で長辺 MAX_IMAGE_DIMENSION 付近まで間引く。
        # これで巨大写真でも全画素を展開せずに済み、メモリ急増を防ぐ。
        try:
            src.draft("RGB", (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
        except Exception:
            pass
        img = src.convert("RGB")  # EXIF回転やCMYK/PNG透過を正規化

        if max(img.size) > MAX_IMAGE_DIMENSION:
            # thumbnail は元画像を破棄しながら縮小するため resize より省メモリ
            img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        data = buf.getvalue()
        buf.close()
        return base64.standard_b64encode(data).decode("utf-8"), "image/jpeg"
    except Exception:
        # 万が一画像処理に失敗した場合は元画像をそのまま使う
        return base64.standard_b64encode(image_data).decode("utf-8"), fallback_media_type
    finally:
        for _im in (img, src):
            try:
                if _im is not None:
                    _im.close()
            except Exception:
                pass
        gc.collect()  # 大きなビットマップを即解放してメモリ上限超過を防ぐ

# ── 利用ログDB（PostgreSQL 優先、なければ SQLite）──────────
_db_lock = threading.Lock()
JST = datetime.timezone(datetime.timedelta(hours=9))
BUILD_ID = "2026-07-11-ramen-min-b2"

import sqlite3   # SQLite は常にフォールバック用に読み込む
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.db")

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
if _DATABASE_URL:
    # Render は "postgres://" で渡してくる場合があるので修正
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql://", 1)

# 既定はSQLite。DATABASE_URLがあり、かつ実際に接続できたときだけPostgreSQLを使う。
# 【重要】本番の顧客データはPostgreSQL(Neon)側にある。Neonはコールドスタートで
# 起動直後の接続が失敗しやすいため、SQLiteへ即フォールバックすると空のローカルDBを
# 見てしまい「履歴が全部消えた」ように見える。実際にはデータはNeonに残っているので、
# ①起動時にリトライし、②起動後もPostgreSQLへ自動再接続（自己修復）する。
USE_PG = False
PH = "?"              # sqlite3 のプレースホルダ
_PG_AVAILABLE = False  # psycopg2 が import できたか
_last_pg_retry = 0.0

if _DATABASE_URL:
    try:
        import psycopg2
        _PG_AVAILABLE = True
    except Exception as _e:
        print(f"[DB][WARN] psycopg2 を読み込めません。SQLiteで継続します: {_e}")


def _try_connect_pg():
    """PostgreSQL への接続を1回試す。成功したら接続を返し、失敗したら None。"""
    global USE_PG, PH
    if not (_DATABASE_URL and _PG_AVAILABLE):
        return None
    try:
        conn = psycopg2.connect(_DATABASE_URL, connect_timeout=15)
        if not USE_PG:
            USE_PG = True
            PH = "%s"     # psycopg2 のプレースホルダ
            print("[DB] PostgreSQL に接続しました")
        return conn
    except Exception as _e:
        print(f"[DB][WARN] PostgreSQL に接続できません: {_e}")
        return None


# 起動時：Neonのコールドスタートを見越して数回リトライしてから判断する
if _DATABASE_URL and _PG_AVAILABLE:
    _RETRY_WAITS = [1, 2, 4, 8, 12]   # 最後の試行後は待たない
    for _attempt in range(len(_RETRY_WAITS) + 1):
        _c = _try_connect_pg()
        if _c is not None:
            _c.close()
            break
        if _attempt < len(_RETRY_WAITS):
            _wait = _RETRY_WAITS[_attempt]
            print(f"[DB] PostgreSQL 再試行 {_attempt + 1}/{len(_RETRY_WAITS)}（{_wait}秒後）")
            time.sleep(_wait)
    if not USE_PG:
        print("[DB][WARN] 起動時はPostgreSQLに接続できませんでした。"
              "SQLiteで暫定継続し、以降のリクエストで自動再接続を試みます。")


def _get_conn():
    global _last_pg_retry
    # まだPostgreSQLに繋がっていないが、DATABASE_URLはある場合、
    # 一定間隔で自動再接続を試みる（Neonが後から起き上がっても実データへ復帰できる）。
    if not USE_PG and _DATABASE_URL and _PG_AVAILABLE:
        now = time.time()
        if now - _last_pg_retry > 30:
            _last_pg_retry = now
            conn = _try_connect_pg()
            if conn is not None:
                return conn
    if USE_PG:
        return psycopg2.connect(_DATABASE_URL, connect_timeout=15)
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
                    id          SERIAL PRIMARY KEY,
                    user_id     TEXT   NOT NULL,
                    date        TEXT   NOT NULL,
                    b_count     REAL   NOT NULL,
                    chara_score REAL,
                    created_at  TEXT   NOT NULL,
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_meals (
                    id         SERIAL PRIMARY KEY,
                    user_id    TEXT   NOT NULL,
                    date       TEXT   NOT NULL,
                    payload    TEXT   NOT NULL,
                    created_at TEXT   NOT NULL,
                    UNIQUE(user_id, date)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_exercise (
                    id         SERIAL PRIMARY KEY,
                    user_id    TEXT   NOT NULL,
                    date       TEXT   NOT NULL,
                    ex_b_count REAL   NOT NULL,
                    created_at TEXT   NOT NULL,
                    UNIQUE(user_id, date)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_profile (
                    user_id      TEXT    PRIMARY KEY,
                    display_name TEXT,
                    is_vip       BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at   TEXT    NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS staff_comments (
                    id         SERIAL PRIMARY KEY,
                    user_id    TEXT NOT NULL,
                    date       TEXT NOT NULL,
                    comment    TEXT NOT NULL,
                    created_at TEXT NOT NULL
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
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     TEXT    NOT NULL,
                    date        TEXT    NOT NULL,
                    b_count     REAL    NOT NULL,
                    chara_score REAL,
                    created_at  TEXT    NOT NULL,
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_meals (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT    NOT NULL,
                    date       TEXT    NOT NULL,
                    payload    TEXT    NOT NULL,
                    created_at TEXT    NOT NULL,
                    UNIQUE(user_id, date)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_exercise (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT    NOT NULL,
                    date       TEXT    NOT NULL,
                    ex_b_count REAL    NOT NULL,
                    created_at TEXT    NOT NULL,
                    UNIQUE(user_id, date)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_profile (
                    user_id      TEXT    PRIMARY KEY,
                    display_name TEXT,
                    is_vip       INTEGER NOT NULL DEFAULT 0,
                    updated_at   TEXT    NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS staff_comments (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT NOT NULL,
                    date       TEXT NOT NULL,
                    comment    TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
        # メール設定を保存するテーブル
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # 端末連携コード（デスクトップ⇔スマホで同じ記録を共有するための一時コード）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS link_codes (
                code       TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        # ひとこと日記（体重/日記タブで入力・履歴に表示。1日1件・50字まで）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_diary (
                user_id    TEXT NOT NULL,
                date       TEXT NOT NULL,
                diary      TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, date)
            )
        """)
        # 修正希望・ご要望の受信箱（管理ダッシュボードから送信者本人へ個別に返信できる）
        # reply_text=管理者からの返信 / replied_at=返信日時 / read_at=本人がアプリで読んだ日時
        if USE_PG:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id         SERIAL PRIMARY KEY,
                    user_id    TEXT NOT NULL,
                    name       TEXT,
                    text       TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reply_text TEXT,
                    replied_at TEXT,
                    read_at    TEXT
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT NOT NULL,
                    name       TEXT,
                    text       TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reply_text TEXT,
                    replied_at TEXT,
                    read_at    TEXT
                )
            """)
        conn.commit()
        # 既存DBへの列追加（デイリーレポートの目的別集計用）。列が既にあれば無視。
        for col in ("gender", "goal"):
            try:
                cur.execute(f"ALTER TABLE user_profile ADD COLUMN {col} TEXT")
                conn.commit()
            except Exception:
                conn.rollback()
        # 既存DBへの列追加（キャラカレンダー用：その日の進捗スコア）。列が既にあれば無視。
        try:
            cur.execute("ALTER TABLE daily_b_count ADD COLUMN chara_score REAL")
            conn.commit()
        except Exception:
            conn.rollback()
    finally:
        conn.close()


try:
    init_db()
except Exception as _e:
    # DB初期化に失敗してもアプリ自体は起動させる（記録は端末側にも保存されている）
    print(f"[DB][WARN] init_db に失敗しました: {_e}")


_VALID_GENDERS = ("male", "female")
_VALID_GOALS   = ("cut", "maintain", "bulk")


def _log_usage(uid):
    """利用ログを1件記録する。DBが一時的に落ちていても解析本体は続行できるよう、
    失敗しても例外を投げずに握りつぶす（記録はあくまで補助的な情報のため）。"""
    uid = (uid or "").strip()
    if not uid:
        return
    ts = datetime.datetime.now(JST).isoformat()
    try:
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
    except Exception as e:
        app.logger.warning("usage_log insert failed (non-fatal): %s", e)


def _save_user_goal(uid, gender, goal):
    """会員の性別・目標(減量/維持/増量)を user_profile へ保存する（レポートの目的別集計用）。
    食事解析リクエストに毎回付いてくる値を随時保存するため、既存会員も
    プロフィールを保存し直さなくても次回の利用時に自動で反映される。"""
    uid    = (uid or "").strip()
    gender = gender if gender in _VALID_GENDERS else None
    goal   = goal   if goal   in _VALID_GOALS   else None
    if not uid or (gender is None and goal is None):
        return
    ts = datetime.datetime.now(JST).isoformat()
    try:
        with _db_lock:
            conn = _get_conn()
            try:
                cur = conn.cursor()
                if USE_PG:
                    cur.execute(
                        """INSERT INTO user_profile (user_id, gender, goal, updated_at)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (user_id) DO UPDATE SET
                             gender     = COALESCE(EXCLUDED.gender, user_profile.gender),
                             goal       = COALESCE(EXCLUDED.goal,   user_profile.goal),
                             updated_at = EXCLUDED.updated_at""",
                        (uid, gender, goal, ts)
                    )
                else:
                    cur.execute(
                        """INSERT INTO user_profile (user_id, gender, goal, updated_at)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(user_id) DO UPDATE SET
                             gender     = COALESCE(excluded.gender, user_profile.gender),
                             goal       = COALESCE(excluded.goal,   user_profile.goal),
                             updated_at = excluded.updated_at""",
                        (uid, gender, goal, ts)
                    )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass  # 集計用の付帯情報のため、保存に失敗しても解析処理は続行する


_backfill_done = False
_backfill_started = False  # バックグラウンド復元を二重起動しないためのフラグ


def _parse_meal_payload_total(payload):
    """daily_meals の payload から、その日の食事Bカウント合計を算出する。
    payload = { mealKey: { items: [ { result: { total_b_count: N } } ] } }。
    旧データの複数食事キー（breakfast/lunch/...）も合算する。"""
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except Exception:
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    total = 0.0
    for _mk, mv in data.items():
        if not isinstance(mv, dict):
            continue
        items = mv.get("items")
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            res = it.get("result")
            if isinstance(res, dict) and res.get("total_b_count") is not None:
                try:
                    total += float(res.get("total_b_count") or 0)
                except (TypeError, ValueError):
                    pass
    return total


def backfill_bcount_from_meals():
    """障害で取りこぼした日ごとのBカウントを、食事明細(daily_meals)から再計算して復元する。
    既存の daily_b_count は上書きせず、まだ無い(user_id, date)だけを補完する（非破壊）。
    PostgreSQL(本番データ)に接続しているときだけ実行する。"""
    global _backfill_done
    if _backfill_done or not USE_PG:
        return
    try:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT user_id, date, payload FROM daily_meals")
            meal_rows = cur.fetchall()
            cur.execute("SELECT user_id, date, ex_b_count FROM daily_exercise")
            ex_map = {(r[0], r[1]): (r[2] or 0) for r in cur.fetchall()}
            cur.execute("SELECT user_id, date FROM daily_b_count")
            existing = {(r[0], r[1]) for r in cur.fetchall()}
            ts = datetime.datetime.now(JST).isoformat()
            inserted = 0
            for uid, date, payload in meal_rows:
                if (uid, date) in existing:
                    continue  # 既存の値は尊重して上書きしない
                food_total = _parse_meal_payload_total(payload)
                if food_total <= 0:
                    continue
                net = max(0.0, food_total - float(ex_map.get((uid, date), 0) or 0))
                cur.execute(
                    f"""INSERT INTO daily_b_count (user_id, date, b_count, created_at)
                        VALUES ({PH}, {PH}, {PH}, {PH})
                        ON CONFLICT (user_id, date) DO NOTHING""",
                    (uid, date, net, ts),
                )
                inserted += 1
            conn.commit()
            print(f"[BACKFILL] 食事明細から {inserted} 件のBカウント履歴を復元しました")
        finally:
            conn.close()
        _backfill_done = True
    except Exception as _e:
        print(f"[BACKFILL][WARN] Bカウント履歴の復元に失敗: {_e}")


# 起動時、本番DBに繋がっていれば取りこぼし分を自動復元する
try:
    backfill_bcount_from_meals()
except Exception as _e:
    print(f"[BACKFILL][WARN] 起動時復元に失敗: {_e}")

# ── 管理者認証 ──────────────────────────────────────────────
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "abDiet2024admin")

# 管理画面のURL。/admin だと推測されやすく会員に存在を知られてしまうため、
# 専用のパスにする（環境変数 ADMIN_URL_PATH で変更可能）。
# 旧 /admin 系のURLはすべて404になり、外からは存在が分からない。
ADMIN_URL_PATH = os.environ.get("ADMIN_URL_PATH", "reall-kanri").strip("/")
ADMIN_BASE = f"/{ADMIN_URL_PATH}"

# 食事分析1回あたりの概算コスト（円）。通常解析はSonnet 4.6（入力$3/出力$15）。
# 画像1024px・出力約1000トークンで約$0.025≒¥4。為替やモデルを変えたら調整。
COST_PER_ANALYSIS_JPY = float(os.environ.get("COST_PER_ANALYSIS_JPY", "4"))

def _is_admin_authed():
    """Basic認証（従来のデスクトップ向け）と X-Admin-Password ヘッダー
    （スマホ向け・管理画面のログインフォームが付与）の両方を受け付ける。"""
    auth = request.authorization
    if auth and auth.password == ADMIN_PASSWORD:
        return True
    if request.headers.get("X-Admin-Password") == ADMIN_PASSWORD:
        return True
    return False


def _admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _is_admin_authed():
            # 管理APIは未認証だと404を返す（401だと「管理APIが存在する」こと自体が
            # 分かってしまうため。管理画面のログインフォームは r.ok で判定するので
            # 404でも問題なく動く）。管理ページ（バックアップ等）はBasic認証を促す。
            if request.path.startswith("/api/"):
                return jsonify({"error": "not found"}), 404
            return Response(
                "認証が必要です",
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
【手順2】カロリーは“目分量”にせず、銘柄が分かるものは「既知の内容量（g/ml）× カロリー密度」または栄養成分表示から必ず計算する。
   その計算結果（kcal）を reason 欄に残し、B食材については以下の基準でカウントする：
   (a) 120〜200kcal → B 0.5カウント
   (b) 200〜400kcal → B 1カウント
   (c) 400kcalを超える → B 2カウント（さらに大きければ0.5刻みで増やす）
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
- 卵（鶏卵・全卵）（100gあたり約142〜150kcal）← 200kcal以下だが【例外的にB食材】として扱う（脂質・コレステロールが多いため）

- 納豆（100gあたり約190〜200kcal）← 【例外的にB食材】として扱う

※【卵の特例】卵（鶏卵・全卵・ゆで卵・目玉焼き・卵焼き・スクランブルエッグ等）は、カロリー密度が200kcal以下でも必ずB食材として扱う。A食材にはしないこと。
  卵1個（約50g・約75kcal）を基準に、【食べた個数ぶんを必ず合算した合計kcal】でSTEP2を適用する。
  1個ずつ別々に判定して各120kcal未満→B0にしてはいけない（過小評価になる）。複数個は必ず1品にまとめて合計で判定する。
  ・1個（約75kcal）→ 120kcal未満 → B0
  ・2個（約150kcal）→ 120〜200kcal → B0.5
  ・3個（約225kcal）→ 200kcal超 → B1
  ・卵焼き・厚焼き（卵2〜3個分＋砂糖・油）→ 約150〜250kcal → B0.5〜B1
  例）目玉焼き2個なら「50g×2＝100g × 約150kcal/100g ＝ 約150kcal → 120〜200 → B0.5」と計算してから判定する。

※【納豆の特例】納豆は必ずB食材として扱う（A食材にしない）。1パック（約40〜50g・約80〜100kcal）を基準に、実際に食べた量の実kcalでSTEP2を適用する（1パックのみなら120kcal未満でB0、複数パックで120kcal以上ならB0.5〜）。

【肉・魚・豆・乳製品以外の分類ルール（1人前の標準カロリーで判定）】
- A食材：1人前の標準カロリーが120kcal未満 → Bカウント0
- B食材：1人前の標準カロリーが120kcal以上 → 実際のkcalでSTEP2を適用

A食材例：
- 野菜全般（生・加熱・炒め・茹でいずれも）、きのこ類、海藻類
- 果物のほとんど（りんご・キウイ・みかん・オレンジ・いちご・ぶどう・なし・もも・すいか・メロン・ベリー類など）
  → 通常量では1人前120kcal未満のためA食材＝Bカウント0。
  【唯一の例外】バナナだけはB食材として扱い、食べた本数の実kcalでSTEP2判定する（下記B食材例を参照）。
  ※アボカドは果物ではなく高脂質のため別扱い（1個約250kcal → B食材寄り。量で判定）。
- お茶・ブラックコーヒー・水
- 調味料少量（塩・醤油・酢・ハーブ・スパイスなど）
- プロテインパウダー単体（牛乳で割らないもの）
- 上記A食材のみで作った手作りスムージー

B食材例：
- 白米・玄米・チャーハン（1杯150g≒250kcal）
- パン全般（食パン・全粒粉パンなど）
- 麺類（そば・うどん・ラーメン・パスタなど、1人前≒270〜350kcal）
- 芋類（さつまいも・じゃがいもなど、100g≒100〜130kcal）
- バナナ（果物だが必ずB食材として扱う。1本約80〜100kcal。食べた本数の実kcalでSTEP2判定する）
  ・1本（約80〜100kcal）→ 120kcal未満 → B0
  ・大きめ1本や1.5本（約120〜200kcal）→ B0.5
  ・2本以上（約200kcal超）→ B1
  ※他のB食材と同じ「120未満→0／120〜200→0.5／200超→1」を、必ず実際の本数・kcalで計算してから適用する。
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
2. 写真内に基準物（手・指・コップ・箸・スプーン・皿・丼など）が写っている場合は、
   それらと食材のサイズを必ず対比させ、料理名の「標準量」ではなく実測に近い量を推定する：
   - 成人の手のひら（指を除く）≒ 縦10〜11cm・横7〜8cm、人差し指の幅 ≒ 1.5〜2cm、握りこぶし ≒ 直径約8〜10cm
   - 箸の幅 ≒ 5mm、箸の長さ ≒ 22〜23cm、一般的な皿の直径 ≒ 20〜26cm
   - 茶碗の直径 ≒ 11〜12cm、ラーメン丼の直径 ≒ 18〜22cm
   - コップ／湯のみの直径 ≒ 7〜8cm、ティースプーン ≒ 5ml、大さじ ≒ 15ml、500mlペットボトルの高さ ≒ 21cm
   ※ 同じ料理でも店や盛り付けで量は大きく異なる。基準物がある場合は標準量を鵜呑みにせず、
     見えているサイズ比からグラム数を補正すること（標準より明らかに大きい/小さいならその通りに増減）。
3. 薄切り・少量の食材は特に過大評価しやすい。「枚数 × 1枚あたりの重量」で計算する：
   - ベーコン薄切り1枚 ≒ 8〜10g（約16〜20kcal）。例）1枚だけなら約10g・約18kcal。
   - ハム1枚 ≒ 10g、ロースハム薄切り1枚 ≒ 約20kcal
   - スライスチーズ1枚 ≒ 18g（約60kcal）、ベビーチーズ1個 ≒ 15g
   - 薬味・ねぎ・ごま・少量のソースなどは「ごく少量」として扱う
4. 各食材について必ず「推定グラム数 → 推定kcal」を明示し、その実kcalでSTEP2を適用する。
   amount欄とreason欄に推定グラム数と推定kcalを必ず書くこと。
5. 量が少ないときは遠慮なく少量と判定してよい（0カウント・0.5カウント・A食材化を恐れない）。
6.【主食（ご飯・麺・パン）は逆に過小評価しないこと・最重要】
   白米・雑穀米・丼・麺・パンなどの主食は、量の見誤りがBカウントを大きく左右する（並盛B1 ↔ 半人前B0.5/B0）。
   「なんとなく半人前」で片付けず、器の満たされ具合から実量を必ず計算すること：
   - 茶碗（直径11〜12cm）に普通によそった量＝並盛 ≒ 150g（約250kcal）→ 200kcal超 → B1
   - 茶碗の半分程度＝半膳 ≒ 75g（約125kcal）→ 120〜200kcal → B0.5
   - 器の底に少しだけ ≒ 50g（約83kcal）→ 120kcal未満 → B0
   - 大盛り・丼によそった量 ≒ 200〜250g（約330〜420kcal）→ B1〜B1.5
   ※ 器の縁近くまで、または山盛りに見えるなら並盛以上（＝最低B1）。安易に0.5人前へ丸めない。
   ※ 麺類（うどん・そば・ラーメン・パスタ）は1人前でも約270〜350kcalあり、丼・皿に普通に入っていれば最低B1。
   ※ 迷ったら「半分」ではなく「並盛」を基準にし、明らかに少ないときだけ半膳以下へ下げる。

【ベーコンの量別の判定例】
- ベーコン1枚（約10g・約18kcal）→ 120kcal未満 → 0カウント
- ベーコン3枚（約30g・約55kcal）→ 120kcal未満 → 0カウント
- ベーコン1人前（約60g・約110kcal）→ 120kcal未満 → 0カウント
- ベーコン大量（約120g・約220kcal）→ 200kcal超 → 1カウント
※ベーコンは100gあたり約185kcal（A食材寄り）だが脂質が多いため、写真の実量で必ず判定する。

【チャーシューの量を実測に近づける例（ラーメン丼の直径や箸を基準に）】
チャーシューは店ごとに枚数・厚み・直径が大きく異なるため、標準70gで固定せず写真のサイズから推定する。
- 薄切り小ぶり3枚（直径6cm程度・薄い）→ 約50〜60g（約110〜130kcal）→ 120〜200kcalなら0.5、未満なら0
- 標準的な3枚（直径7〜8cm）→ 約70g（約150kcal）→ 120〜200kcal → 0.5カウント
- 厚切り・大判3枚（直径9cm超・厚みあり、丼の半分を覆う）→ 約120〜150g（約260〜320kcal）→ 200kcal超 → 1カウント
※チャーシュー（豚バラ）は100gあたり約210kcal前後。丼の直径や箸の太さと対比して直径・厚みを見積もり、グラム数を補正すること。

━━━━━━━━━━━━━━━━━━━━━━━
■ STEP2：B食材のBカウント計算【最重要】
━━━━━━━━━━━━━━━━━━━━━━━

料理名でカウントせず、食材ごとに分解して個別に判定する。
写真に写っている実際の量のカロリーを推定し、以下で判定する。
（1品でも大盛り・高カロリーなら 1.5・2… とカウントしてよい。上限は設けない）

- 実際のカロリーが120kcal未満       → 0カウント（ノーカウント）
- 実際のカロリーが120〜200kcal      → 0.5カウント
- 実際のカロリーが200〜400kcal      → 1カウント
- 実際のカロリーが400kcalを超える   → 2カウント
  （例：うどん大盛りが単体で400kcalを超える → その1品だけで B2）
- さらに大きい場合の目安：600kcal超→2.5、800kcal超→3 のように0.5刻みで増やす
※ この「実際のkcal→カウント」ルールは、うどんに限らず全てのB食材に同じように適用する。

【最重要・二重計上の絶対禁止（全食材共通）】
同じ食材・同じカロリーは、foods配列の中で必ず「1項目・1回だけ」計上する。
ある項目のkcal・b_countの判定に含めたカロリーを、別の項目として重ねて計上してはいけない。
- 誤りの例：「かぼちゃの天ぷら」の判定に吸収油270kcalを合算してBを付け、さらに「かぼちゃ天ぷら吸収油」を別項目でB1計上する（油を2回数えている）
- 正しい例：「かぼちゃの天ぷら」は本体のみ（102kcal→0）、「揚げ油（吸収油）」は油のみ（270kcal→B1）と分け、それぞれ1回ずつ判定する
- 油に限らず、ご飯・麺・ソース・チーズなども同様。1つのカロリーがどの項目に属するかを決めたら、他の項目には含めない。

━━━━━━━━━━━━━━━━━━━━━━━
■【最重要】B食材は必ず「計算してから」判定する（量の見誤り防止）
━━━━━━━━━━━━━━━━━━━━━━━
量を“なんとなく”でカウントせず、B食材は1品ずつ必ず次の計算を声に出して行ってから判定すること：

  手順：① 量（g または ml）を見積もる → ② カロリー密度（100gあたり・100mlあたりのkcal）を当てる
       → ③ 実際のkcal ＝ 量 × 密度 を計算する → ④ そのkcalを120/200のしきい値に当てて 0/0.5/1 を決める

そして reason 欄には、必ずこの計算式を残すこと。
  例）「350ml × 45kcal/100ml ＝ 約158kcal → 120〜200 → B0.5」
  例）「ご飯 茶碗1杯 約150g × 168kcal/100g ＝ 約250kcal → 200超 → B1」
計算式（量×密度＝kcal）を書かずにカウントだけを出すのは禁止。難しいときは時間をかけて慎重に計算してよい。

━━━━━━━━━━━━━━━━━━━━━━━
■ 具体例
━━━━━━━━━━━━━━━━━━━━━━━

【白米】※器の満たされ具合で必ず実量を見積もる。並盛を安易に半人前へ丸めないこと
・少量（50g、約83kcal）→ 120kcal未満 → 0カウント
・半膳（75g、約125kcal）→ 120〜200kcal → 0.5カウント
・並盛1杯＝茶碗に普通によそった量（150g、約250kcal）→ 200kcal超 → 1カウント
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

【ラーメン】※1人前を食べていれば最低でも合計B2として計算する
① 麺1人前（約270〜300kcal）→ 200kcal超 → B1
② スープの油（1人前のスープには油が多く、大さじ2杯＝約30g≒270kcalが入っている前提）→ 200kcal超 → B1
   ※油が少なめのあっさり系でも、スープの油は最低でも大さじ2杯分として必ず計上する
③ チャーシューは量に応じて適宜Bを加算する（例：標準2〜3枚は0〜B0.5、脂身の多い厚切り・多め＝100g前後で約210kcal→B1）
→ 合計 最低 B2（＋チャーシューの量に応じて加算）

━━━━━━━━━━━━━━━━━━━━━━━
■ 揚げ物のルール（主食材と吸収油は"別項目"として1回ずつ判定）
━━━━━━━━━━━━━━━━━━━━━━━
- 主食材：STEP1で分類し、**油を含めない本体のみ**の実kcalでSTEP2を適用（1個が小さければ120kcal未満で0になる）
- 衣（小麦粉・卵・パン粉）：常にノーカウント（量が少ないため）
- 吸収油：食材重量 × 吸油率 × 9kcal で実kcalを計算し、「揚げ油（吸収油）」という**独立した1項目**としてSTEP2で判定（油1g≒9kcal）

【絶対禁止・油の二重計上】吸収油のkcalは「吸収油の項目」でのみ計上する。
主食材の項目のkcal・b_countに吸収油を合算してはいけない（主食材は本体のkcalのみ）。
「主食材＋油を合算して判定した項目」と「吸収油の項目」を両方出すと同じ油を2回数えることになるので絶対にしないこと。

【最重要】皿の上に揚げ物が複数ある場合（天ぷら盛り合わせ・複数の天ぷら・唐揚げ数個など）は、
油を1個ずつ別々に判定して過小評価してはいけない。**全ての揚げ物の吸収油を必ず合算し、
その合計kcalを"1つの油"としてSTEP2で判定する**（例：合計120g × 吸油率20% ＝ 24g ＝ 216kcal → 200kcal超 → B1）。
主食材は各自の実kcalで別途判定するが、海老・イカ・ちくわ・少量の鶏など1個が小さいものは120kcal未満で0になることが多い。

【吸油率の目安】※揚げ物の吸油率は原則20%を使う（迷ったら20%）
- から揚げ・竜田揚げ → 約20%
- 魚介の天ぷら → 約20%
- 天ぷら盛り合わせ（種類が混在）→ 約20%
- とんかつ・フライ（パン粉の厚い衣）→ 約20%
- 野菜の天ぷら・かき揚げ（油を多く吸う）→ 約25〜30%

【計算例】
- 鶏もも唐揚げ1人前（100g）
  ① 鶏もも肉（B食材、100gで約204kcal）→ 200kcal超 → 1カウント
  ② 衣 → 0カウント
  ③ 吸油：100g × 20% ≒ 20g（180kcal）→ 120〜200kcal → 0.5カウント
  ✅ 合計：B1.5カウント → 各食材の最大は1なので B1（鶏もも）+ B0.5（吸油）= B1.5カウント

- 鶏むね唐揚げ1人前（100g）
  ① 鶏むね肉（A食材、100gで約105kcal）→ 0カウント
  ② 衣 → 0カウント
  ③ 吸油：100g × 20% ≒ 20g（180kcal）→ 120〜200kcal → 0.5カウント
  ✅ 合計：B0.5カウント

- 野菜の天ぷら3個（合計100g）
  ① 野菜 → A食材 → 0カウント
  ② 衣 → 0カウント
  ③ 吸油：100g × 28% ≒ 28g（252kcal）→ 200kcal超 → 1カウント
  ✅ 合計：B1カウント

- うどん＋ミニ天丼＋天ぷら盛り合わせ（定食）※このパターンは必ずこの計算に合わせる
  ① うどん麺1人前（約270〜300kcal）→ 200kcal超 → B1
  ② ミニ天丼の白米（約140g・約233kcal）→ 200kcal超 → B1
  ③ 揚げ物（海老天・イカ天・ちくわ天・鶏天・かぼちゃ天など）
     ・主食材：海老/イカ/ちくわ/少量の鶏は1個が小さく各120kcal未満 → 0
     ・吸収油：揚げ物の合計を約120gとし、120g × 吸油率20% ＝ 24g ＝ 216kcal → 200kcal超 → B1（油をまとめて1つで計上）
  ✅ 合計：B3カウント（うどんB1＋白米B1＋揚げ油B1）

━━━━━━━━━━━━━━━━━━━━━━━
■ ドリンクのルール
━━━━━━━━━━━━━━━━━━━━━━━
飲み物も同じルールを適用する。写真に写っているボトル・缶の実際の容量から摂取カロリーを推定し、STEP2で判定する。

【最重要】銘柄・パッケージが分かる飲料・商品は“目分量”にせず、必ず「標準容量 × カロリー密度 ＝ kcal」を計算して判定する。
よく出る商品の標準値（迷ったらこの表で計算する）：
- モンスターエナジー（355ml・約45kcal/100ml）＝ 約160kcal → 120〜200 → B0.5　※「45kcal/100ml × 350〜355ml」で必ず計算
- レッドブル（250ml・約46kcal/100ml）＝ 約115kcal → 120未満 → 0／大缶355ml＝約163kcal → B0.5
- 加糖炭酸（コーラ等・約45kcal/100ml）：250ml≒113kcal→0／350ml缶≒158kcal→B0.5／500mlPET≒225kcal→B1
- 加糖の缶コーヒー（185ml・約38kcal/100ml）＝約70kcal→0（微糖はさらに低い）
- スポーツドリンク（約23kcal/100ml）：500ml≒115kcal→0〜B0.5
- 果汁100%ジュース（約45kcal/100ml）：200ml≒90kcal→0／500ml≒225kcal→B1
- ビール（約40kcal/100ml）：350ml缶≒140kcal→B0.5／500ml缶≒200kcal→B0.5〜1

- お茶・ブラックコーヒー・水・無糖炭酸 → A食材 → 0カウント
- 上記以外の清涼飲料水・ジュース・アルコール → 容量×密度でkcalを計算しSTEP2で判定
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
※ただし油の項目は1つの料理につき1回だけ。揚げ物で「吸収油」をすでに計上した料理に、さらに「調理油」を重ねて計上しない（二重計上禁止）。
- ドレッシング（大さじ1.5程度、約165kcal）→ 0.5カウント

━━━━━━━━━━━━━━━━━━━━━━━
■ 品数が多い料理（寿司・弁当・ビュッフェ等）の出力ルール
━━━━━━━━━━━━━━━━━━━━━━━
- 同じ食材・同じネタはまとめて1項目にする（例：まぐろの握り3貫→「まぐろの握り ×3貫」で1項目）。1貫ずつ個別に列挙しないこと。
- foods配列は最大でも15項目程度に収め、reasonは簡潔に（計算式「量×密度=kcal→判定」は残すが、余計な説明は書かない）。
- 出力が長くなりすぎないよう注意し、JSONは必ず最後まで完結させること（途中で切らない）。
※ 寿司の例：シャリはB食材（1貫のシャリ約15〜20g）。ネタ（まぐろ・サーモン等）と合わせて握り単位でまとめ、握り全体のkcalで判定する。

━━━━━━━━━━━━━━━━━━━━━━━
■ タンパク質(g)の推定ルール
━━━━━━━━━━━━━━━━━━━━━━━
各食材について、実際に食べた量ぶんの推定タンパク質量(g)を protein_g に整数で入れる。
おおよその目安（100gあたりのタンパク質）：
- 鶏むね/ささみ≒23g、鶏もも≒18g、牛・豚赤身≒20g、ひき肉≒17g
- 魚（鮭・まぐろ・ぶり等）≒20g、卵1個(50g)≒6g、木綿豆腐≒7g、納豆1パック≒8g
- 牛乳100ml≒3g、ヨーグルト100g≒4g、チーズ≒22g、プロテイン1杯≒20g
- ご飯100g≒2.5g、パン・麺≒8g/100g、白米茶碗1杯(150g)≒4g
- 野菜・果物・油・砂糖・調味料 ≒ 0〜1g（ほぼ0）
写真の量から実摂取量を見積もって計算する。全食材の合計を total_protein_g に整数で入れる。

━━━━━━━━━━━━━━━━━━━━━━━
■ 野菜(g)の推定ルール
━━━━━━━━━━━━━━━━━━━━━━━
各食材について、実際に食べた野菜の重量(g)を veg_g に整数で入れる（野菜・きのこ・海藻のみ）。
- 対象：葉物・根菜・果菜・きのこ・海藻など一般的な野菜（生・加熱問わず実重量）
- 対象外（veg_g=0）：いも類（じゃがいも・さつまいも等）・豆類・果物・米麦などの穀物・肉魚卵・乳製品・油
分量の目安：小鉢のサラダ≒40〜60g、サラダ1皿≒100g、野菜炒め1人前≒120〜150g、
具だくさん味噌汁の野菜≒40〜60g、ラーメンの野菜トッピング≒30〜80g、トマト1個≒150g。
全食材の合計を total_veg_g に整数で入れる。

━━━━━━━━━━━━━━━━━━━━━━━
■ 果物(g)の推定ルール
━━━━━━━━━━━━━━━━━━━━━━━
各食材について、実際に食べた果物の重量(g)を fruit_g に整数で入れる（果物のみ。果物以外はすべて0）。
- 対象：生・冷凍・缶詰・ドライフルーツなどの果物そのもの（トマト・アボカドは野菜扱いで0）
- 対象外（fruit_g=0）：果汁ジュース・ジャム・果物味の菓子や飲料
分量の目安：バナナ1本≒100g、りんご1個≒250g、みかん1個≒80g、いちご5粒≒75g、キウイ1個≒85g、ぶどう1房≒150g。
全食材の合計を total_fruit_g に整数で入れる。

━━━━━━━━━━━━━━━━━━━━━━━
■ 確認質問（clarify）— 量が写真から判定しにくいときのフラグ
━━━━━━━━━━━━━━━━━━━━━━━
写真だけでは量の判定が難しい場合、JSONの clarify フィールドで「ユーザーに確認したい」ことを伝える：
1. staple.needed=true にする条件：白米・ご飯・パン・麺類などの主食が写っていて、量に少しでも不確かさがあるとき（原則 true にする）。
   - 丼もの・カレー・チャーハン・定食のご飯・麺類は、具や器で量が隠れて写真から正確に判定できないため【必ず true】。
   - 「並盛〜やや多め」のように少しでも大盛りが疑われる場合も【必ず true】。
   - false にしてよいのは、量が確定できる場合だけ：おにぎり1個・パン1枚など個数で分かるもの、栄養成分表示が写っているもの、主食が明らかに少量（100g未満）の場合。
   - food_name にはその主食名（例：「白米」「パスタ」「うどん」）を入れ、その主食の amount 欄には「やや大盛り」等の見た目の量も書くこと。
2. oil.needed=true にする条件：炒め物・揚げ物・アヒージョ・オイル系ドレッシングなど、調理油が大さじ0.5以上使われている可能性があるが、油の量が写真から読み取れないとき。
3. どちらも該当しない場合は needed=false にする。
4. clarify を出す場合でも、foods には現時点で最も妥当な推定（標準的な盛り・標準的な油量）で必ず計上しておくこと。

必ず以下のJSON形式のみで回答してください。JSONの前後に説明文やコードブロックは不要です：
【重要】categoryフィールドは "A" または "B" のみ使用すること。"Good B"・"グッドB" は使用禁止。
{
  "foods": [
    {
      "name": "食材名（具体的に）",
      "category": "A",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "protein_g": この食材の推定タンパク質量（g・整数。実際の摂取量ぶん。肉魚卵乳大豆等は多め、野菜・油・砂糖等はほぼ0）,
      "veg_g": この食材のうち野菜類の重量（g・整数。実際の摂取量ぶん。野菜・きのこ・海藻のみ。いも・豆・果物・穀物・肉魚等は0）,
      "fruit_g": この食材のうち果物の重量（g・整数。実際の摂取量ぶん。果物のみ。果物以外は0）,
      "amount": "写真での量（例：たっぷり、1人前、少量など）",
      "b_count": 0,
      "reason": "A食材のため（1人前約○kcal、120kcal未満）Bカウント0"
    },
    {
      "name": "食材名（具体的に）",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "protein_g": この食材の推定タンパク質量（g・整数。実際の摂取量ぶん。肉魚卵乳大豆等は多め、野菜・油・砂糖等はほぼ0）,
      "veg_g": この食材のうち野菜類の重量（g・整数。実際の摂取量ぶん。野菜・きのこ・海藻のみ。いも・豆・果物・穀物・肉魚等は0）,
      "fruit_g": この食材のうち果物の重量（g・整数。実際の摂取量ぶん。果物のみ。果物以外は0）,
      "amount": "写真での量と推定kcal（例：1人前・約250kcal）",
      "b_count": 1,
      "reason": "B食材（1人前約○kcal）、実際の摂取量約○kcalが200kcal超のためB1"
    },
    {
      "name": "食材名（具体的に）",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "protein_g": この食材の推定タンパク質量（g・整数。実際の摂取量ぶん。肉魚卵乳大豆等は多め、野菜・油・砂糖等はほぼ0）,
      "veg_g": この食材のうち野菜類の重量（g・整数。実際の摂取量ぶん。野菜・きのこ・海藻のみ。いも・豆・果物・穀物・肉魚等は0）,
      "fruit_g": この食材のうち果物の重量（g・整数。実際の摂取量ぶん。果物のみ。果物以外は0）,
      "amount": "写真での量と推定kcal（例：約150kcal相当）",
      "b_count": 0.5,
      "reason": "B食材（1人前約○kcal）、実際の摂取量約○kcalが120〜200kcalのためB0.5"
    },
    {
      "name": "食材名（具体的に）",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "protein_g": この食材の推定タンパク質量（g・整数。実際の摂取量ぶん。肉魚卵乳大豆等は多め、野菜・油・砂糖等はほぼ0）,
      "veg_g": この食材のうち野菜類の重量（g・整数。実際の摂取量ぶん。野菜・きのこ・海藻のみ。いも・豆・果物・穀物・肉魚等は0）,
      "fruit_g": この食材のうち果物の重量（g・整数。実際の摂取量ぶん。果物のみ。果物以外は0）,
      "amount": "写真での量と推定kcal（例：少量・約70kcal）",
      "b_count": 0,
      "reason": "B食材だが実際の摂取量約○kcalが120kcal未満のためノーカウント"
    }
  ],
  "total_b_count": 合計Bカウント（数値）,
  "total_protein_g": 全食材の推定タンパク質の合計（g・整数）,
  "total_veg_g": 全食材の野菜の合計（g・整数）,
  "total_fruit_g": 全食材の果物の合計（g・整数）,
  "advice": "このメニューをABダイエット観点でのワンポイントアドバイス（1〜2文）",
  "clarify": {
    "staple": { "needed": trueまたはfalse, "food_name": "主食名（neededがtrueのときのみ）" },
    "oil": { "needed": trueまたはfalse }
  }
}"""


def get_client():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    # タイムアウトとリトライを明示する：
    #  - timeout=140秒 …… 具材の多い複雑な写真でもAIが解析し切れるよう十分な時間を確保する
    #    （短すぎるとタイムアウトが多発してエラーになる）。gunicornの --timeout=300秒より
    #    十分に短くし、下記のcreate_and_parseの再試行（最大2回×140秒＝280秒）でも
    #    ワーカーが強制終了される前に必ずクライアント側で打ち切れるようにする。
    #  - max_retries=0 …… SDK側の自動再試行は無効化する。再試行はcreate_and_parseが
    #    アプリ側でまとめて行うため、二重に再試行して総待ち時間がgunicornの上限(300秒)を
    #    超えてワーカーが落とされる（＝500/502の原因）のを防ぐ。
    return anthropic.Anthropic(api_key=key, timeout=140.0, max_retries=0)


@app.route("/")
def index():
    return render_template("index.html")


@app.route(ADMIN_BASE)
def admin():
    # ページ自体は認証なしで表示し、データを返すAPI側で認証する。
    # （スマホではBasic認証ダイアログが正しく動かないことが多いため、
    #   ページ内のログインフォームでパスワードを入力してもらう方式）
    # URLは推測されにくい専用パス（ADMIN_BASE）。旧 /admin は404になる。
    return render_template("admin.html", admin_base=ADMIN_BASE)


@app.route("/api/admin/auth-check")
@_admin_required
def admin_auth_check():
    """管理画面のログインフォームがパスワードを確認するための軽量API"""
    return jsonify({"ok": True})


@app.route("/api/admin/overview")
@_admin_required
def admin_overview():
    now = datetime.datetime.now(JST)
    today_start = now.strftime("%Y-%m-%dT00:00:00")
    yesterday_start = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    month_start_dt = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM usage_log")
        total_users = cur.fetchone()[0] or 0

        # 今日・前日のアクティブユーザー数（その日に1回でも解析した人数）
        cur.execute(
            f"SELECT COUNT(DISTINCT user_id) FROM usage_log WHERE created_at >= {PH}",
            (today_start,),
        )
        today_active = cur.fetchone()[0] or 0

        cur.execute(
            f"SELECT COUNT(DISTINCT user_id) FROM usage_log WHERE created_at >= {PH} AND created_at < {PH}",
            (yesterday_start, today_start),
        )
        yesterday_active = cur.fetchone()[0] or 0

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
        "today_active": today_active,
        "yesterday_active": yesterday_active,
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

        # 減量メンバーだけの日別平均Bカウント（ダッシュボードのグラフ用）
        cur.execute(
            f"""SELECT b.date, AVG(b.b_count)
               FROM daily_b_count b
               JOIN user_profile p ON p.user_id = b.user_id AND p.goal = 'cut'
               WHERE b.date >= {PH}
               GROUP BY b.date ORDER BY b.date""",
            (start_str,),
        )
        cut_b_by_day = {row[0]: round(float(row[1]), 2) for row in cur.fetchall()}
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
        "cut_avg_b_counts": [cut_b_by_day.get(dt) for dt in all_dates],
    })


@app.route("/api/admin/report-data")
@_admin_required
def admin_report_data():
    """毎日8時メールレポート用の集計データを返す。"""
    now = datetime.datetime.now(JST)
    # 8時実行時は前日が対象日
    target_date = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    target_start = f"{target_date}T00:00:00"
    target_end   = f"{target_date}T23:59:59"
    start_30d    = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    start_30d_ts = f"{start_30d}T00:00:00"

    conn = _get_conn()
    try:
        cur = conn.cursor()

        # ── デイリー利用統計 ──────────────────────────────────
        cur.execute(
            f"SELECT COUNT(DISTINCT user_id) FROM usage_log WHERE created_at>={PH} AND created_at<={PH}",
            (target_start, target_end),
        )
        daily_users = cur.fetchone()[0] or 0

        cur.execute(
            f"SELECT COUNT(*) FROM usage_log WHERE created_at>={PH} AND created_at<={PH}",
            (target_start, target_end),
        )
        daily_analyses = cur.fetchone()[0] or 0

        # ── 時間帯別分布（UTC+9 の時刻で記録されているため文字列の12-13文字目が時） ──
        cur.execute(
            f"""SELECT SUBSTR(created_at,12,2) AS hr, COUNT(*) AS cnt
               FROM usage_log WHERE created_at>={PH} AND created_at<={PH}
               GROUP BY hr ORDER BY hr""",
            (target_start, target_end),
        )
        hourly_raw = {row[0]: row[1] for row in cur.fetchall()}
        hourly = [hourly_raw.get(f"{h:02d}", 0) for h in range(24)]

        # ── 食事記録集計（daily_meals payloadをパース） ──
        # アプリは「朝食/昼食/夕食/間食」の4スロットを廃止し、単一の food
        # カテゴリへ統合済み（旧データ互換のため4キーも合わせて読む）。
        cur.execute(
            f"SELECT user_id, payload FROM daily_meals WHERE date={PH}",
            (target_date,),
        )
        meal_rows = cur.fetchall()
        MEAL_KEYS = ("food", "breakfast", "lunch", "dinner", "snack")
        meal_count = meal_photo = meal_text = 0
        meal_users = set()
        for uid, payload_str in meal_rows:
            try:
                payload = json.loads(payload_str)
                user_has_record = False
                for key in MEAL_KEYS:
                    items = payload.get(key, {}).get("items", [])
                    for item in items:
                        if not item.get("result"):
                            continue
                        meal_count += 1
                        user_has_record = True
                        if item.get("isText"):
                            meal_text += 1
                        else:
                            meal_photo += 1
                if user_has_record:
                    meal_users.add(uid)
            except Exception:
                pass
        meal_summary = {
            "count": meal_count, "photo": meal_photo, "text": meal_text,
            "users": len(meal_users),
        }

        # ── 30日トレンド（集団平均） ──────────────────────────
        # 【重要】上限は前日(target_date)まで。当日は朝だけの途中データで
        # 棒グラフが極端に短くなり平均を乱すため、レポートには含めない。
        cur.execute(
            f"""SELECT date, AVG(b_count) FROM daily_b_count
               WHERE date>={PH} AND date<={PH} GROUP BY date ORDER BY date""",
            (start_30d, target_date),
        )
        b_avg_by_day = {row[0]: round(float(row[1]), 2) for row in cur.fetchall()}

        cur.execute(
            f"""SELECT date, AVG(weight) FROM daily_weight
               WHERE date>={PH} AND date<={PH} GROUP BY date ORDER BY date""",
            (start_30d, target_date),
        )
        w_avg_by_day = {row[0]: round(float(row[1]), 2) for row in cur.fetchall()}

        cur.execute(
            f"""SELECT date, AVG(ex_b_count) FROM daily_exercise
               WHERE date>={PH} AND date<={PH} GROUP BY date ORDER BY date""",
            (start_30d, target_date),
        )
        ex_avg_by_day = {row[0]: round(float(row[1]), 2) for row in cur.fetchall()}

        # ── 個人別トレンド（スパゲッティプロット用） ──────────
        cur.execute(
            f"SELECT user_id, date, b_count FROM daily_b_count WHERE date>={PH} AND date<={PH} ORDER BY user_id, date",
            (start_30d, target_date),
        )
        b_by_user: dict = {}
        for uid, dt, b in cur.fetchall():
            b_by_user.setdefault(uid, {})[dt] = b

        cur.execute(
            f"SELECT user_id, date, weight FROM daily_weight WHERE date>={PH} AND date<={PH} ORDER BY user_id, date",
            (start_30d, target_date),
        )
        w_by_user: dict = {}
        for uid, dt, w in cur.fetchall():
            w_by_user.setdefault(uid, {})[dt] = w

        # ── 減量実績（初回記録 vs 最新記録、全期間。ただし最新は前日まで） ──────────
        cur.execute(
            f"SELECT user_id, date, weight FROM daily_weight WHERE date<={PH} ORDER BY user_id, date",
            (target_date,),
        )
        all_w_by_user: dict = {}
        for uid, dt, w in cur.fetchall():
            all_w_by_user.setdefault(uid, []).append((dt, w))

        weight_loss_list = []
        loss_by_user_day: dict = {}
        for uid, rows in all_w_by_user.items():
            if len(rows) < 2:
                continue
            initial_w = rows[0][1]
            latest_w  = rows[-1][1]
            weight_loss_list.append(initial_w - latest_w)
            # プラス値 = 初回記録からの減量幅（体重が減っているほど大きくなる）
            loss_by_user_day[uid] = {dt: (initial_w - w) for dt, w in rows}

        weight_loss_avg_kg = (
            round(sum(weight_loss_list) / len(weight_loss_list), 2)
            if weight_loss_list else None
        )
        weight_loss_users = len(weight_loss_list)

        # ── 日別利用推移（前日まで） ──────────────────────────
        cur.execute(
            f"""SELECT SUBSTR(created_at,1,10) AS dt,
                       COUNT(DISTINCT user_id), COUNT(*)
               FROM usage_log WHERE created_at>={PH} AND created_at<={PH}
               GROUP BY dt ORDER BY dt""",
            (start_30d_ts, target_end),
        )
        usage_by_day: dict = {}
        for dt, users, analyses in cur.fetchall():
            usage_by_day[dt] = {"users": users, "analyses": analyses}

        # ── 全体平均Bカウント（直近30日・前日まで・1人1日あたり） ──────
        cur.execute(
            f"SELECT AVG(b_count) FROM daily_b_count WHERE date>={PH} AND date<={PH}",
            (start_30d, target_date),
        )
        row = cur.fetchone()
        b_overall_avg = round(float(row[0]), 2) if row and row[0] is not None else None

        # ── 会員の目標（減量/維持/増量）マップ ────────────────
        goal_by_user: dict = {}
        try:
            cur.execute("SELECT user_id, goal FROM user_profile WHERE goal IS NOT NULL")
            goal_by_user = {r[0]: r[1] for r in cur.fetchall()}
        except Exception:
            pass  # goal列が無い旧スキーマでも動くように

        # ── 栄養素集計用に直近30日の食事明細を取得（前日まで） ────────────
        cur.execute(
            f"SELECT user_id, date, payload FROM daily_meals WHERE date>={PH} AND date<={PH}",
            (start_30d, target_date),
        )
        nutrition_rows = cur.fetchall()

    finally:
        conn.close()

    # 30日間の全日付リスト（前日=target_dateまで。当日は途中データのため含めない）
    all_dates = []
    d = (now - datetime.timedelta(days=30)).date()
    end_d = (now - datetime.timedelta(days=1)).date()
    while d <= end_d:
        all_dates.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)

    individual_b = [
        {"uid": uid[:5] + "…", "values": [dates_dict.get(d) for d in all_dates]}
        for uid, dates_dict in b_by_user.items()
    ]
    individual_w = [
        {"uid": uid[:5] + "…", "values": [dates_dict.get(d) for d in all_dates]}
        for uid, dates_dict in w_by_user.items()
    ]
    individual_loss = [
        {"uid": uid[:5] + "…", "values": [dates_dict.get(d) for d in all_dates]}
        for uid, dates_dict in loss_by_user_day.items()
    ]
    loss_avg_by_day = {}
    for d in all_dates:
        vals = [dates_dict[d] for dates_dict in loss_by_user_day.values() if d in dates_dict]
        if vals:
            loss_avg_by_day[d] = round(sum(vals) / len(vals), 2)

    # ── 栄養素（タンパク質・野菜・果物）の集計 ────────────────
    def _day_nutrition(payload_str):
        """1人1日ぶんの食事payloadから (タンパク質g, 野菜g, 果物g or None) を返す。
        果物は計測開始前の記録にはフィールド自体が無いため、未計測(None)と0gを区別する。"""
        try:
            payload = json.loads(payload_str)
        except Exception:
            return None
        protein = veg = 0.0
        fruit = None
        has_result = False
        for key in MEAL_KEYS:
            for item in payload.get(key, {}).get("items", []):
                res = item.get("result")
                if not isinstance(res, dict):
                    continue
                has_result = True
                foods = res.get("foods") or []
                p = res.get("total_protein_g")
                protein += float(p) if isinstance(p, (int, float)) else sum(float(f.get("protein_g") or 0) for f in foods)
                v = res.get("total_veg_g")
                veg += float(v) if isinstance(v, (int, float)) else sum(float(f.get("veg_g") or 0) for f in foods)
                fr = res.get("total_fruit_g")
                if not isinstance(fr, (int, float)) and any(isinstance(f, dict) and "fruit_g" in f for f in foods):
                    fr = sum(float(f.get("fruit_g") or 0) for f in foods)
                if isinstance(fr, (int, float)):
                    fruit = (fruit or 0.0) + float(fr)
        if not has_result:
            return None
        return (protein, veg, fruit)

    def _avg(vals, nd=1):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), nd) if vals else None

    nut_by_day: dict = {}    # date -> [(p, v, fruit), ...]（1要素=1人の1日合計）
    nut_by_user: dict = {}   # uid  -> [(p, v, fruit), ...]
    for uid, dt, payload_str in nutrition_rows:
        tot = _day_nutrition(payload_str)
        if tot is None:
            continue
        nut_by_day.setdefault(dt, []).append(tot)
        nut_by_user.setdefault(uid, []).append(tot)

    all_user_days = [t for day in nut_by_day.values() for t in day]
    nutrition = {
        "protein_avg": _avg([t[0] for t in all_user_days]),
        "veg_avg":     _avg([t[1] for t in all_user_days]),
        "fruit_avg":   _avg([t[2] for t in all_user_days]),
        # 果物データが付いた記録日数（プロンプト更新後に増えていく。0なら「収集中」表示）
        "fruit_days":  len([t for t in all_user_days if t[2] is not None]),
        "users":       len(nut_by_user),
        "protein_avg_trend": [_avg([t[0] for t in nut_by_day.get(d, [])]) for d in all_dates],
        "veg_avg_trend":     [_avg([t[1] for t in nut_by_day.get(d, [])]) for d in all_dates],
        "fruit_avg_trend":   [_avg([t[2] for t in nut_by_day.get(d, [])]) for d in all_dates],
    }

    # ── 目的別比較（減量/維持/増量ごとの平均B・タンパク質・野菜） ──
    goal_compare = {}
    for gkey in ("cut", "maintain", "bulk"):
        uids = {u for u, g in goal_by_user.items() if g == gkey}
        b_vals = [b for u in uids for b in b_by_user.get(u, {}).values()]
        goal_compare[gkey] = {
            "users":       len(uids),
            "avg_b":       round(sum(b_vals) / len(b_vals), 2) if b_vals else None,
            "avg_protein": _avg([t[0] for u in uids for t in nut_by_user.get(u, [])]),
            "avg_veg":     _avg([t[1] for u in uids for t in nut_by_user.get(u, [])]),
        }

    # ── 減量希望者：平均Bカウント×平均減量の推移（相関グラフ用） ──
    cut_uids = {u for u, g in goal_by_user.items() if g == "cut"}
    cut_corr = {
        "users": len(cut_uids),
        "b_avg_trend": [
            _avg([b_by_user[u][d] for u in cut_uids if u in b_by_user and d in b_by_user[u]], 2)
            for d in all_dates
        ],
        "loss_avg_trend": [
            _avg([loss_by_user_day[u][d] for u in cut_uids if u in loss_by_user_day and d in loss_by_user_day[u]], 2)
            for d in all_dates
        ],
    }

    return jsonify({
        "report_date":          target_date,
        "daily_users":          daily_users,
        "daily_analyses":       daily_analyses,
        "est_cost_jpy":         round(daily_analyses * COST_PER_ANALYSIS_JPY),
        "cost_per_analysis":    COST_PER_ANALYSIS_JPY,
        "meal_summary":         meal_summary,
        "hourly":               hourly,
        "dates":                all_dates,
        "b_avg_trend":          [b_avg_by_day.get(d) for d in all_dates],
        "w_avg_trend":          [w_avg_by_day.get(d) for d in all_dates],
        "ex_avg_trend":         [ex_avg_by_day.get(d) for d in all_dates],
        "daily_users_trend":    [usage_by_day.get(d, {}).get("users", 0) for d in all_dates],
        "daily_analyses_trend": [usage_by_day.get(d, {}).get("analyses", 0) for d in all_dates],
        "individual_b":         individual_b,
        "individual_w":         individual_w,
        "weight_loss_avg_kg":   weight_loss_avg_kg,
        "weight_loss_users":    weight_loss_users,
        "loss_avg_trend":       [loss_avg_by_day.get(d) for d in all_dates],
        "individual_loss":      individual_loss,
        "b_overall_avg":        b_overall_avg,
        "nutrition":            nutrition,
        "goal_compare":         goal_compare,
        "cut_corr":             cut_corr,
    })


@app.route("/api/admin/cut-members")
@_admin_required
def admin_cut_members():
    """ダッシュボード用：減量メンバーの詳細（直近7日の日別Bカウント・最終記録日）。
    誰が記録をサボっているか・毎日どれくらいBを摂っているかを一覧で確認する。"""
    days = 7
    now = datetime.datetime.now(JST)
    dates = [(now.date() - datetime.timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    date_start = dates[0]

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, display_name, is_vip FROM user_profile WHERE goal = 'cut'")
        cut_users = {r[0]: {"name": r[1], "is_vip": bool(r[2])} for r in cur.fetchall()}
        if not cut_users:
            return jsonify({"dates": dates, "members": []})

        cur.execute(
            f"""SELECT user_id, date, b_count FROM daily_b_count
               WHERE date >= {PH} ORDER BY date""",
            (date_start,)
        )
        recent = {}
        for uid, date, bc in cur.fetchall():
            if uid in cut_users:
                recent.setdefault(uid, {})[date] = float(bc)

        cur.execute("SELECT user_id, MAX(date) FROM daily_b_count GROUP BY user_id")
        last_by_uid = {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()

    today = now.date()
    members = []
    for uid, prof in cut_users.items():
        daily = recent.get(uid, {})
        vals = list(daily.values())
        last = last_by_uid.get(uid)
        days_since = (today - datetime.date.fromisoformat(last)).days if last else None
        members.append({
            "user_id":       uid,
            "user_id_short": uid[:8],
            "name":          prof["name"] or None,
            "is_vip":        prof["is_vip"],
            "daily_b":       {d: daily.get(d) for d in dates},
            "avg_b_7d":      round(sum(vals) / len(vals), 1) if vals else None,
            "recorded_days_7d": len(vals),
            "last_record":   last,
            "days_since":    days_since,
        })
    # 記録が途絶えている人が上に来るように並べる（未記録→日数が長い順）
    members.sort(key=lambda m: (-(m["days_since"] if m["days_since"] is not None else 9999),
                                m["name"] or "zzz"))
    return jsonify({"dates": dates, "members": members})


@app.route("/api/admin/weight-insights")
@_admin_required
def admin_weight_insights():
    """ダッシュボード用：目的別の平均体重変化と、Bカウント×体重変化の相関データ。
    直近30日の体重記録がある会員について、期間内の最初と最後の体重の差を「変化」とする。"""
    days = 30
    now = datetime.datetime.now(JST)
    date_start = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    date_end   = now.strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT user_id, date, weight FROM daily_weight
               WHERE date>={PH} AND date<={PH} ORDER BY user_id, date""",
            (date_start, date_end)
        )
        w_rows = cur.fetchall()
        cur.execute(
            f"""SELECT user_id, AVG(b_count) FROM daily_b_count
               WHERE date>={PH} AND date<={PH} GROUP BY user_id""",
            (date_start, date_end)
        )
        b_rows = cur.fetchall()
        cur.execute("SELECT user_id, display_name, goal FROM user_profile")
        p_rows = cur.fetchall()
    finally:
        conn.close()

    avg_b_by_uid = {r[0]: float(r[1]) for r in b_rows if r[1] is not None}
    prof_by_uid  = {r[0]: {"name": r[1], "goal": r[2]} for r in p_rows}

    # ユーザーごとに期間内の最初・最後の体重から変化量を計算
    weights_by_uid = {}
    for uid, date, weight in w_rows:
        weights_by_uid.setdefault(uid, []).append((date, weight))

    users = []
    for uid, recs in weights_by_uid.items():
        if len(recs) < 2:
            continue
        first_d, first_w = recs[0]
        last_d,  last_w  = recs[-1]
        span = (datetime.date.fromisoformat(last_d) - datetime.date.fromisoformat(first_d)).days
        if span < 5:
            continue   # 記録期間が短すぎる場合はノイズが大きいので除外
        prof = prof_by_uid.get(uid, {})
        users.append({
            "user_id_short": uid[:8],
            "name":   prof.get("name") or None,
            "goal":   prof.get("goal") or None,
            "change": round(float(last_w) - float(first_w), 2),
            "avg_b":  round(avg_b_by_uid[uid], 2) if uid in avg_b_by_uid else None,
            "span_days": span,
        })

    # 目的別の集計（減量/維持/増量/未設定）
    goals = {}
    for key in ("cut", "maintain", "bulk", "none"):
        grp = [u for u in users if (u["goal"] or "none") == key]
        if grp:
            changes = [u["change"] for u in grp]
            goals[key] = {
                "users":      len(grp),
                "avg_change": round(sum(changes) / len(changes), 2),
                "lost":       sum(1 for c in changes if c < -0.05),
            }
        else:
            goals[key] = {"users": 0, "avg_change": None, "lost": 0}

    # Bカウント×体重変化の相関（ピアソンのr）。
    # 減量目的の会員だけを対象にする（維持・増量の人は体重を減らす意図がなく、
    # 相関を薄めてしまうため、散布図からも除外する）。
    pts = [u for u in users if u["avg_b"] is not None and u["goal"] == "cut"]
    corr = None
    if len(pts) >= 3:
        xs = [u["avg_b"] for u in pts]
        ys = [u["change"] for u in pts]
        n = len(pts)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        if sxx > 0 and syy > 0:
            corr = round(sxy / (sxx * syy) ** 0.5, 2)

    return jsonify({
        "period_days": days,
        "date_start":  date_start,
        "date_end":    date_end,
        "goals":       goals,
        "scatter":     pts,
        "correlation": corr,
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

        # 目標(goal)も取得（旧スキーマにgoal列が無い場合に備えて分岐）
        try:
            cur.execute("SELECT user_id, display_name, is_vip, goal FROM user_profile")
            profiles = {
                row[0]: {"display_name": row[1], "is_vip": bool(row[2]), "goal": row[3]}
                for row in cur.fetchall()
            }
        except Exception:
            cur.execute("SELECT user_id, display_name, is_vip FROM user_profile")
            profiles = {
                row[0]: {"display_name": row[1], "is_vip": bool(row[2]), "goal": None}
                for row in cur.fetchall()
            }

        # 各会員の「開始時期の体重」と「最新の体重」を集計（daily_weightの最古・最新）
        cur.execute("SELECT user_id, date, weight FROM daily_weight ORDER BY user_id, date")
        weight_by_user: dict = {}
        for uid_w, dt_w, wt_w in cur.fetchall():
            rec = weight_by_user.setdefault(uid_w, {})
            if "initial" not in rec:
                rec["initial"] = float(wt_w)
                rec["start_date"] = dt_w
            rec["latest"] = float(wt_w)
    finally:
        conn.close()

    GOAL_LABELS = {"cut": "減量希望", "maintain": "体重維持", "bulk": "増量希望"}

    all_ids = set(b_stats.keys()) | set(analysis_counts.keys()) | set(profiles.keys())
    users = []
    for uid in all_ids:
        bs = b_stats.get(uid, {})
        pf = profiles.get(uid, {})
        wr = weight_by_user.get(uid, {})
        initial_w = wr.get("initial")
        latest_w  = wr.get("latest")
        change = round(latest_w - initial_w, 1) if (initial_w is not None and latest_w is not None) else None
        goal = pf.get("goal")
        users.append({
            "user_id": uid,
            "user_id_short": uid[:8] + "…",
            "display_name": pf.get("display_name"),
            "is_vip": pf.get("is_vip", False),
            "goal": goal,
            "goal_label": GOAL_LABELS.get(goal, "未設定"),
            "days_recorded": bs.get("days", 0),
            "avg_b_count": bs.get("avg_b"),
            "analyses_count": analysis_counts.get(uid, 0),
            "last_active": bs.get("last_active", ""),
            "initial_weight": initial_w,
            "latest_weight": latest_w,
            "weight_start_date": wr.get("start_date"),
            "weight_change": change,
        })

    # 並び順：減量希望なのに痩せていない人（体重が減っていない）を最上位にする。
    #  tier 0 … 減量希望 かつ 体重変化が判明していて 0以上（増えた/変わらない）＝要注目
    #  tier 1 … 減量希望 かつ 体重が減っている（変化<0）
    #  tier 2 … 減量希望 だが体重データがまだ無い
    #  tier 3 … それ以外（体重維持・増量・目標未設定）
    # 同tier内は、体重変化が大きい（増えている）順→記録が新しい順。
    def _sort_key(u):
        g = u["goal"]
        ch = u["weight_change"]
        if g == "cut" and ch is not None and ch >= 0:
            tier = 0
        elif g == "cut" and ch is not None and ch < 0:
            tier = 1
        elif g == "cut":
            tier = 2
        else:
            tier = 3
        change_rank = -(ch if ch is not None else -9999)  # 増えているほど上
        return (tier, change_rank)
    # 先に「最終利用が新しい順」で並べ、その後に安定ソートで tier / 体重変化を優先する
    users.sort(key=lambda x: x["last_active"] or "", reverse=True)
    users.sort(key=_sort_key)

    return jsonify({"users": users})


@app.route("/api/profile", methods=["POST"])
def api_profile():
    """会員がアプリの設定画面で入力した表示名を保存（スタッフ確認用）。
    性別・目標（減量/維持/増量）が付いていれば、レポートの目的別集計用に合わせて保存する。"""
    data = request.get_json(silent=True) or {}
    uid  = (data.get("user_id") or "").strip()
    name = (data.get("display_name") or "").strip()[:30]
    if not uid:
        return jsonify({"error": "パラメータ不足"}), 400

    if name:
        ts = datetime.datetime.now(JST).isoformat()
        with _db_lock:
            conn = _get_conn()
            try:
                cur = conn.cursor()
                if USE_PG:
                    cur.execute(
                        """INSERT INTO user_profile (user_id, display_name, updated_at)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (user_id) DO UPDATE SET display_name=%s, updated_at=%s""",
                        (uid, name, ts, name, ts)
                    )
                else:
                    cur.execute(
                        """INSERT INTO user_profile (user_id, display_name, updated_at)
                           VALUES (?, ?, ?)
                           ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at""",
                        (uid, name, ts)
                    )
                conn.commit()
            finally:
                conn.close()
    _save_user_goal(uid, data.get("gender") or "", data.get("goal") or "")
    return jsonify({"ok": True})


@app.route("/api/profile", methods=["GET"])
def api_profile_get():
    """端末連携時に、連携先アカウントのプロフィールを取得する。"""
    uid = (request.args.get("user_id") or "").strip()
    if not uid:
        return jsonify({"error": "パラメータ不足"}), 400
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT display_name, gender, goal FROM user_profile WHERE user_id={PH}",
            (uid,)
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"display_name": None, "gender": None, "goal": None})
    return jsonify({"display_name": row[0], "gender": row[1], "goal": row[2]})


# ═══════════════════════════════════════════
#  端末連携（デスクトップ⇔スマホで同じ記録を共有）
#  片方の端末で6桁コードを発行し、もう片方で入力すると
#  同じユーザーIDになり、記録が全端末で共有される。
# ═══════════════════════════════════════════
LINK_CODE_TTL_MIN = 10


@app.route("/api/link-code", methods=["POST"])
def api_link_code():
    """連携コードを発行する（10分間有効）。"""
    data = request.get_json(silent=True) or {}
    uid = (data.get("user_id") or "").strip()
    if not uid:
        return jsonify({"error": "パラメータ不足"}), 400

    now = datetime.datetime.now(JST)
    expires = (now + datetime.timedelta(minutes=LINK_CODE_TTL_MIN)).isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            # 期限切れコードを掃除
            cur.execute(f"DELETE FROM link_codes WHERE expires_at < {PH}", (now.isoformat(),))
            # 衝突しない6桁コードを生成
            code = None
            for _ in range(20):
                candidate = f"{secrets.randbelow(1000000):06d}"
                cur.execute(f"SELECT 1 FROM link_codes WHERE code={PH}", (candidate,))
                if not cur.fetchone():
                    code = candidate
                    break
            if not code:
                conn.commit()
                return jsonify({"error": "コードの発行に失敗しました。もう一度お試しください。"}), 500
            cur.execute(
                f"INSERT INTO link_codes (code, user_id, expires_at) VALUES ({PH}, {PH}, {PH})",
                (code, uid, expires)
            )
            conn.commit()
        finally:
            conn.close()
    return jsonify({"code": code, "expires_minutes": LINK_CODE_TTL_MIN})


@app.route("/api/link-redeem", methods=["POST"])
def api_link_redeem():
    """連携コードを使って、コード発行元のユーザーIDを取得する。"""
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "コードを入力してください"}), 400

    now_iso = datetime.datetime.now(JST).isoformat()
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT user_id, expires_at FROM link_codes WHERE code={PH}",
            (code,)
        )
        row = cur.fetchone()
        if not row or row[1] < now_iso:
            return jsonify({"error": "コードが見つからないか、期限切れです。もう一度発行してください。"}), 404
        uid = row[0]
        cur.execute(
            f"SELECT display_name, gender, goal FROM user_profile WHERE user_id={PH}",
            (uid,)
        )
        prof = cur.fetchone()
    finally:
        conn.close()
    return jsonify({
        "user_id": uid,
        "profile": {
            "display_name": prof[0] if prof else None,
            "gender":       prof[1] if prof else None,
            "goal":         prof[2] if prof else None,
        },
    })


@app.route("/api/admin/users/<user_id>/vip", methods=["POST"])
@_admin_required
def admin_set_vip(user_id):
    data = request.get_json(silent=True) or {}
    is_vip = bool(data.get("is_vip"))
    ts = datetime.datetime.now(JST).isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            if USE_PG:
                cur.execute(
                    """INSERT INTO user_profile (user_id, is_vip, updated_at)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET is_vip=%s, updated_at=%s""",
                    (user_id, is_vip, ts, is_vip, ts)
                )
            else:
                cur.execute(
                    """INSERT INTO user_profile (user_id, is_vip, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET is_vip=excluded.is_vip, updated_at=excluded.updated_at""",
                    (user_id, is_vip, ts)
                )
            conn.commit()
        finally:
            conn.close()
    return jsonify({"ok": True, "is_vip": is_vip})


@app.route(f"{ADMIN_BASE}/user/<user_id>")
def admin_user_page(user_id):
    # ダッシュボードと同様、ページは認証なしで表示しデータAPI側で認証する
    return render_template("admin_user.html", user_id=user_id, admin_base=ADMIN_BASE)


@app.route("/api/admin/user/<user_id>/detail")
@_admin_required
def admin_user_detail(user_id):
    """ユーザー詳細のうち“軽い”データ（プロフィール・体重/Bカウント/運動グラフ・
    コメント）を返す。食事写真（base64サムネで重い）は含めず、別APIで遅延取得する。
    これでページ上部のグラフが即表示され、写真の読み込みを待たずに済む。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()

        cur.execute(f"SELECT display_name, is_vip FROM user_profile WHERE user_id={PH}", (user_id,))
        row = cur.fetchone()
        display_name = row[0] if row else None
        is_vip = bool(row[1]) if row else False

        cur.execute(
            f"SELECT date, weight FROM daily_weight WHERE user_id={PH} ORDER BY date",
            (user_id,),
        )
        weight = [{"date": r[0], "weight": r[1]} for r in cur.fetchall()]

        cur.execute(
            f"SELECT date, b_count FROM daily_b_count WHERE user_id={PH} ORDER BY date",
            (user_id,),
        )
        b_count = [{"date": r[0], "b_count": r[1]} for r in cur.fetchall()]

        cur.execute(
            f"SELECT date, ex_b_count FROM daily_exercise WHERE user_id={PH} ORDER BY date",
            (user_id,),
        )
        exercise = [{"date": r[0], "ex_b_count": r[1]} for r in cur.fetchall()]

        cur.execute(
            f"SELECT id, date, comment, created_at FROM staff_comments WHERE user_id={PH} ORDER BY date DESC, created_at DESC",
            (user_id,),
        )
        comments = [
            {"id": r[0], "date": r[1], "comment": r[2], "created_at": r[3]}
            for r in cur.fetchall()
        ]

        # 食事写真は別APIで取得するが、フロントが枠を用意できるよう「記録のある日数」だけ先に返す。
        cur.execute(
            f"SELECT COUNT(*) FROM daily_meals WHERE user_id={PH}",
            (user_id,),
        )
        meal_days = cur.fetchone()[0] or 0
    finally:
        conn.close()

    return jsonify({
        "display_name": display_name,
        "is_vip": is_vip,
        "weight": weight,
        "b_count": b_count,
        "exercise": exercise,
        "comments": comments,
        "meal_days": meal_days,
    })


@app.route("/api/admin/user/<user_id>/meals")
@_admin_required
def admin_user_meals(user_id):
    """食事写真（base64サムネを含む重いデータ）を日付の新しい順に返す。
    ?limit=N&offset=M で分割取得でき、詳細ページの写真セクションを遅延ロードする。"""
    try:
        limit = min(60, max(1, int(request.args.get("limit", 30))))
    except (TypeError, ValueError):
        limit = 30
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT date, payload FROM daily_meals WHERE user_id={PH}
               ORDER BY date DESC LIMIT {PH} OFFSET {PH}""",
            (user_id, limit, offset),
        )
        meal_rows = cur.fetchall()
    finally:
        conn.close()

    MEAL_KEYS = ("food", "breakfast", "lunch", "dinner", "snack")
    meals = []
    for dt, payload_str in meal_rows:
        try:
            payload = json.loads(payload_str)
        except Exception:
            continue
        items = []
        for key in MEAL_KEYS:
            for item in payload.get(key, {}).get("items", []):
                if not item.get("result") and not item.get("isText"):
                    continue
                items.append({
                    "previewSrc": item.get("previewSrc"),
                    "isText": item.get("isText", False),
                    "text": item.get("text"),
                    "result": item.get("result"),
                })
        if items:
            meals.append({"date": dt, "items": items})

    return jsonify({"meals": meals, "limit": limit, "offset": offset, "has_more": len(meal_rows) == limit})


@app.route("/api/admin/user/<user_id>/comment", methods=["POST"])
@_admin_required
def admin_add_comment(user_id):
    data = request.get_json(silent=True) or {}
    date    = (data.get("date") or "").strip()
    comment = (data.get("comment") or "").strip()
    if not date or not comment:
        return jsonify({"error": "パラメータ不足"}), 400

    ts = datetime.datetime.now(JST).isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"INSERT INTO staff_comments (user_id, date, comment, created_at) VALUES ({PH}, {PH}, {PH}, {PH})",
                (user_id, date, comment, ts),
            )
            conn.commit()
        finally:
            conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<user_id>/comment/<int:comment_id>", methods=["DELETE"])
@_admin_required
def admin_delete_comment(user_id, comment_id):
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"DELETE FROM staff_comments WHERE id={PH} AND user_id={PH}",
                (comment_id, user_id),
            )
            conn.commit()
        finally:
            conn.close()
    return jsonify({"ok": True})


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
    """Render無料プランのスリープ防止用エンドポイント。1日1回データバックアップメールを送信する。

    ※ 毎朝8時のアプリ内デイリーレポート（表形式・グラフなしの簡易版）は廃止した。
    　 デイリーレポートは GitHub Actions（report/send_report.py・グラフ付きの詳細版）に一本化する。"""
    now = datetime.datetime.now(JST)
    # 1日1回、全データのバックアップをメール送信（多重送信は関数内で抑止）
    if _backup_check.get("date") != now.date():
        _backup_check["date"] = now.date()
        threading.Thread(target=_maybe_send_backup, daemon=True).start()
    return "ok", 200


@app.route("/version")
def version():
    """デプロイ反映確認用。稼働中コードのビルド識別子を返す。"""
    return jsonify({"build": BUILD_ID, "has_retry": "create_and_parse" in globals()})


# 修正希望・ご要望の転送先（運営メール）
FEEDBACK_TO = "reallgym.tokyo@gmail.com"


@app.route("/api/feedback", methods=["POST"])
def submit_feedback():
    """利用者からの修正希望・ご要望を受信箱（DB）に保存し、メールでも運営へ転送する。
    ダッシュボードの受信箱が主経路のため、メール送信に失敗しても受付は成功扱いにする。"""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    uid = (data.get("user_id") or "").strip()
    if not text:
        return jsonify({"error": "内容を入力してください"}), 400
    if len(text) > 5000:
        text = text[:5000]

    # 表示名を取得（分かれば件名・本文に添える）
    name = ""
    try:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT display_name FROM user_profile WHERE user_id={PH}", (uid,))
            row = cur.fetchone()
            name = (row[0] if row else "") or ""
        finally:
            conn.close()
    except Exception:
        pass

    # まず受信箱（DB）へ保存する。ここが管理ダッシュボードの返信機能の元データになる。
    ts = datetime.datetime.now(JST).isoformat()
    saved = False
    try:
        with _db_lock:
            conn = _get_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    f"INSERT INTO feedback (user_id, name, text, created_at) VALUES ({PH}, {PH}, {PH}, {PH})",
                    (uid, name, text, ts)
                )
                conn.commit()
                saved = True
            finally:
                conn.close()
    except Exception as e:
        print(f"[FEEDBACK][WARN] DB保存に失敗: {e}")

    # メール転送（ベストエフォート。設定が無い・失敗しても受信箱に残っていれば受付成功）
    gmail_user = "".join((_get_setting("GMAIL_USER") or "").split())
    gmail_pass = "".join((_get_setting("GMAIL_APP_PASSWORD") or "").split())
    if not gmail_user or not gmail_pass:
        if saved:
            return jsonify({"ok": True})
        return jsonify({"error": "メール送信の設定が未登録のため送信できません。運営にお問い合わせください。"}), 500

    now = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    body = (
        "ABダイエットアプリに修正希望・ご要望が届きました。\n\n"
        f"日時: {now}\n"
        f"利用者: {name or '(名前未設定)'}\n"
        f"user_id: {uid or '(不明)'}\n\n"
        "──────────────\n"
        f"{text}\n"
        "──────────────\n"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(f"[ABダイエット] 修正希望 - {name or (uid[:8] if uid else '匿名')}", "utf-8")
    msg["From"] = gmail_user
    msg["To"] = FEEDBACK_TO
    msg["Reply-To"] = FEEDBACK_TO
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(gmail_user, gmail_pass)
            smtp.sendmail(gmail_user, [FEEDBACK_TO], msg.as_bytes())
    except Exception as e:
        if not saved:
            return jsonify({"error": f"送信に失敗しました。時間をおいて再度お試しください。({e})"}), 500
        print(f"[FEEDBACK][WARN] メール転送に失敗（受信箱には保存済み）: {e}")
    return jsonify({"ok": True})


# ── 修正希望・ご要望：管理ダッシュボードの受信箱と個別返信 ──────────────


@app.route("/api/admin/feedback")
@_admin_required
def admin_feedback_list():
    """受信箱の一覧（新しい順・最大200件）。返信状況（未返信/未読/既読）も返す。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, user_id, name, text, created_at, reply_text, replied_at, read_at
               FROM feedback ORDER BY created_at DESC LIMIT 200"""
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    items = [
        {"id": r[0], "user_id": r[1], "name": r[2] or "", "text": r[3],
         "created_at": r[4], "reply_text": r[5], "replied_at": r[6], "read_at": r[7]}
        for r in rows
    ]
    return jsonify({"items": items})


@app.route("/api/admin/feedback/<int:fid>/reply", methods=["POST"])
@_admin_required
def admin_feedback_reply(fid):
    """個別返信を保存する。再送信（上書き）すると未読に戻る。空文字で返信の取り消し。"""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if len(text) > 5000:
        text = text[:5000]
    ts = datetime.datetime.now(JST).isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            if text:
                cur.execute(
                    f"UPDATE feedback SET reply_text={PH}, replied_at={PH}, read_at=NULL WHERE id={PH}",
                    (text, ts, fid)
                )
            else:
                cur.execute(
                    f"UPDATE feedback SET reply_text=NULL, replied_at=NULL, read_at=NULL WHERE id={PH}",
                    (fid,)
                )
            updated = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    if not updated:
        return jsonify({"error": "対象が見つかりません"}), 404
    return jsonify({"ok": True})


@app.route("/api/feedback-replies")
def get_feedback_replies():
    """送信者本人向け：自分の問い合わせへの運営からの返信一覧（返信済みのものだけ）。"""
    uid = (request.args.get("user_id") or "").strip()
    if not uid:
        return jsonify({"items": []})
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT id, text, reply_text, replied_at, read_at FROM feedback
               WHERE user_id={PH} AND reply_text IS NOT NULL
               ORDER BY replied_at DESC LIMIT 50""",
            (uid,)
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    items = [
        {"id": r[0], "text": r[1], "reply_text": r[2], "replied_at": r[3], "read": bool(r[4])}
        for r in rows
    ]
    return jsonify({"items": items})


@app.route("/api/feedback-replies/read", methods=["POST"])
def mark_feedback_replies_read():
    """送信者本人が返信を読んだ印を付ける（本人のuser_idと一致する項目のみ）。"""
    data = request.get_json(silent=True) or {}
    uid = (data.get("user_id") or "").strip()
    ids = data.get("ids") or []
    if not uid or not isinstance(ids, list) or not ids:
        return jsonify({"ok": True})
    ids = [int(i) for i in ids if isinstance(i, (int, float, str)) and str(i).lstrip('-').isdigit()][:50]
    if not ids:
        return jsonify({"ok": True})
    ts = datetime.datetime.now(JST).isoformat()
    ph_list = ",".join([PH] * len(ids))
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE feedback SET read_at={PH} WHERE user_id={PH} AND read_at IS NULL AND id IN ({ph_list})",
                tuple([ts, uid] + ids)
            )
            conn.commit()
        finally:
            conn.close()
    return jsonify({"ok": True})


# ── デイリーレポートメール ──────────────────────────────────────
_last_report_sent: dict = {}
_backup_check: dict = {}


def _bar_html(val: int, total: int, color: str) -> str:
    pct = round(val / total * 100) if total else 0
    return (
        f'<div style="width:{pct}%;height:10px;background:{color};'
        f'border-radius:4px;min-width:2px"></div>'
    )


def _build_report_html(target_date: str) -> str:
    now = datetime.datetime.now(JST)
    ts_start = f"{target_date}T00:00:00"
    ts_end   = f"{target_date}T23:59:59"
    start_7d = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    start_30d = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        cur = conn.cursor()

        cur.execute(
            f"SELECT COUNT(DISTINCT user_id) FROM usage_log WHERE created_at>={PH} AND created_at<={PH}",
            (ts_start, ts_end),
        )
        daily_users = cur.fetchone()[0] or 0

        cur.execute(
            f"SELECT COUNT(*) FROM usage_log WHERE created_at>={PH} AND created_at<={PH}",
            (ts_start, ts_end),
        )
        daily_analyses = cur.fetchone()[0] or 0
        est_cost = round(daily_analyses * COST_PER_ANALYSIS_JPY)

        # 時間帯別
        cur.execute(
            f"SELECT SUBSTR(created_at,12,2) AS hr, COUNT(*) FROM usage_log "
            f"WHERE created_at>={PH} AND created_at<={PH} GROUP BY hr ORDER BY hr",
            (ts_start, ts_end),
        )
        hourly = {row[0]: row[1] for row in cur.fetchall()}
        peak_h = max(hourly, key=hourly.get, default="-")
        peak_v = max(hourly.values(), default=0)

        # 食事記録（アプリは朝食/昼食/夕食/間食の4スロットを廃止し food に統合済み。
        # 旧データ互換のため4キーも合わせて読む）
        cur.execute(f"SELECT user_id, payload FROM daily_meals WHERE date={PH}", (target_date,))
        MEAL_KEYS = ("food", "breakfast", "lunch", "dinner", "snack")
        meal_count = meal_photo = meal_text = 0
        meal_users = set()
        for uid, payload_str in cur.fetchall():
            try:
                pl = json.loads(payload_str)
                user_has_record = False
                for key in MEAL_KEYS:
                    for item in pl.get(key, {}).get("items", []):
                        if not item.get("result"):
                            continue
                        meal_count += 1
                        user_has_record = True
                        if item.get("isText"):
                            meal_text += 1
                        else:
                            meal_photo += 1
                if user_has_record:
                    meal_users.add(uid)
            except Exception:
                pass

        # 直近7日 Bカウント・体重・運動
        cur.execute(
            f"SELECT date, AVG(b_count) FROM daily_b_count WHERE date>={PH} GROUP BY date ORDER BY date",
            (start_7d,),
        )
        b_trend = [(r[0][5:], round(float(r[1]), 1)) for r in cur.fetchall()]

        cur.execute(
            f"SELECT date, AVG(weight) FROM daily_weight WHERE date>={PH} GROUP BY date ORDER BY date",
            (start_7d,),
        )
        w_trend = [(r[0][5:], round(float(r[1]), 1)) for r in cur.fetchall()]

        cur.execute(
            f"SELECT date, AVG(ex_b_count) FROM daily_exercise WHERE date>={PH} GROUP BY date ORDER BY date",
            (start_7d,),
        )
        ex_trend = [(r[0][5:], round(float(r[1]), 1)) for r in cur.fetchall()]

        # 直近30日 個人数
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM daily_b_count WHERE date>={0}".format(PH), (start_30d,))
        active_b_users = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM daily_weight WHERE date>={0}".format(PH), (start_30d,))
        active_w_users = cur.fetchone()[0] or 0

    finally:
        conn.close()

    # ── HTML 組み立て ──────────────────────────
    def trend_rows(rows, unit, color):
        if not rows:
            return '<tr><td colspan="3" style="color:#9CA3AF;padding:8px">データなし</td></tr>'
        max_v = max(v for _, v in rows)
        return "".join(
            f'<tr><td style="padding:4px 8px;font-size:13px;color:#6B7280">{d}</td>'
            f'<td style="padding:4px 8px;font-size:14px;font-weight:800;color:#374151;text-align:right">{v} {unit}</td>'
            f'<td style="padding:4px 8px;width:150px"><div style="background:#E5E7EB;border-radius:4px;overflow:hidden;height:8px">'
            f'{_bar_html(int(v * 10), int(max_v * 10), color)}'
            f'</div></td></tr>'
            for d, v in rows
        )

    def hour_rows():
        if not hourly:
            return '<tr><td colspan="3" style="color:#9CA3AF;padding:8px">データなし</td></tr>'
        max_v = max(hourly.values())
        rows = ""
        for h in range(24):
            v = hourly.get(f"{h:02d}", 0)
            if v == 0:
                continue
            rows += (
                f'<tr><td style="padding:3px 8px;font-size:13px;color:#6B7280">{h}:00〜</td>'
                f'<td style="padding:3px 8px;font-size:13px;font-weight:800;text-align:right">{v}回</td>'
                f'<td style="padding:3px 8px;width:150px"><div style="background:#E5E7EB;border-radius:4px;overflow:hidden;height:8px">'
                f'{_bar_html(v, max_v, "#6366F1")}'
                f'</div></td></tr>'
            )
        return rows

    sent_at = now.strftime("%Y-%m-%d %H:%M JST")
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#F3F4F6;font-family:-apple-system,'Helvetica Neue',Arial,sans-serif">
<div style="max-width:620px;margin:0 auto;padding:20px">
  <div style="background:linear-gradient(135deg,#FF6B35,#e55a25);color:#fff;border-radius:14px 14px 0 0;padding:22px 24px">
    <div style="font-size:20px;font-weight:900">🏋️ ABダイエット デイリーレポート</div>
    <div style="font-size:13px;margin-top:6px;opacity:.88">対象日: {target_date}</div>
  </div>
  <div style="background:#fff;padding:24px;border-radius:0 0 14px 14px">

    <!-- 1. 利用統計 -->
    <div style="font-size:14px;font-weight:800;color:#374151;border-left:4px solid #FF6B35;padding-left:10px;margin-bottom:14px">
      📊 利用統計（{target_date}）
    </div>
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px">
      <div style="flex:1;min-width:130px;background:#F9FAFB;border-radius:10px;padding:14px;text-align:center">
        <div style="font-size:11px;color:#6B7280;font-weight:700">デイリー利用者</div>
        <div style="font-size:30px;font-weight:900;color:#FF6B35">{daily_users}</div>
        <div style="font-size:11px;color:#9CA3AF">ユーザー</div>
      </div>
      <div style="flex:1;min-width:130px;background:#F9FAFB;border-radius:10px;padding:14px;text-align:center">
        <div style="font-size:11px;color:#6B7280;font-weight:700">食事分析回数</div>
        <div style="font-size:30px;font-weight:900;color:#FF6B35">{daily_analyses}</div>
        <div style="font-size:11px;color:#9CA3AF">回</div>
      </div>
      <div style="flex:1;min-width:130px;background:#F9FAFB;border-radius:10px;padding:14px;text-align:center">
        <div style="font-size:11px;color:#6B7280;font-weight:700">推定API費用</div>
        <div style="font-size:30px;font-weight:900;color:#FF6B35">¥{est_cost}</div>
        <div style="font-size:11px;color:#9CA3AF">¥{COST_PER_ANALYSIS_JPY}/回</div>
      </div>
    </div>

    <!-- 食事記録 -->
    <div style="font-size:13px;font-weight:700;color:#6B7280;margin-bottom:8px">食事記録（{target_date}）</div>
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px">
      <div style="flex:1;min-width:130px;background:#F9FAFB;border-radius:10px;padding:14px;text-align:center">
        <div style="font-size:11px;color:#6B7280;font-weight:700">記録件数</div>
        <div style="font-size:26px;font-weight:900;color:#FF6B35">{meal_count}</div>
        <div style="font-size:11px;color:#9CA3AF">📸{meal_photo} / 📝{meal_text}</div>
      </div>
      <div style="flex:1;min-width:130px;background:#F9FAFB;border-radius:10px;padding:14px;text-align:center">
        <div style="font-size:11px;color:#6B7280;font-weight:700">記録した人数</div>
        <div style="font-size:26px;font-weight:900;color:#FF6B35">{len(meal_users)}</div>
        <div style="font-size:11px;color:#9CA3AF">ユーザー</div>
      </div>
    </div>

    <!-- 2. Bカウント推移 -->
    <div style="font-size:14px;font-weight:800;color:#374151;border-left:4px solid #FF6B35;padding-left:10px;margin-bottom:14px">
      🍽️ Bカウント推移（直近7日・集団平均）
    </div>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
      {trend_rows(b_trend, "B", "#FF6B35")}
    </table>

    <!-- 3. 体重推移 -->
    <div style="font-size:14px;font-weight:800;color:#374151;border-left:4px solid #FF6B35;padding-left:10px;margin-bottom:14px">
      ⚖️ 体重推移（直近7日・集団平均）
    </div>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
      {trend_rows(w_trend, "kg", "#10B981")}
    </table>

    <!-- 3b. 運動によるBカウント消費 -->
    <div style="font-size:14px;font-weight:800;color:#374151;border-left:4px solid #FF6B35;padding-left:10px;margin-bottom:14px">
      🏃 運動によるBカウント消費（直近7日・集団平均）
    </div>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
      {trend_rows(ex_trend, "B", "#6366F1")}
    </table>
    <div style="font-size:12px;color:#9CA3AF;margin-bottom:24px">
      記録ユーザー数（直近30日）— Bカウント: {active_b_users}名 / 体重: {active_w_users}名
    </div>

    <!-- 4. システム状況 -->
    <div style="font-size:14px;font-weight:800;color:#374151;border-left:4px solid #FF6B35;padding-left:10px;margin-bottom:14px">
      ⚙️ システム・解析状況（{target_date}）
    </div>
    <div style="font-size:13px;color:#374151;margin-bottom:8px">
      📌 解析ピーク時間帯: <strong style="color:#FF6B35">{peak_h}:00〜</strong>（{peak_v}回）
    </div>
    <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
      {hour_rows()}
    </table>
    <div style="background:#F9FAFB;border-radius:10px;padding:12px;font-size:12px;color:#6B7280">
      APIクレジット残高は
      <a href="https://console.anthropic.com/settings/billing" style="color:#FF6B35">Anthropic Console</a>
      でご確認ください。
    </div>

  </div>
  <div style="text-align:center;font-size:11px;color:#9CA3AF;margin-top:16px">
    ABダイエット 自動レポート | 毎朝8:00 JST 配信<br>Generated: {sent_at}
  </div>
</div>
</body></html>"""


def _get_setting(key: str, default: str = "") -> str:
    """DB の settings テーブルから値を取得する。環境変数が優先。"""
    env_val = os.environ.get(key, "").strip()
    if env_val:
        return env_val
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT value FROM settings WHERE key={PH}", (key,))
        row = cur.fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def _set_setting(key: str, value: str):
    """DB の settings テーブルに値を保存する。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if USE_PG:
            cur.execute(
                "INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=%s",
                (key, value, value),
            )
        else:
            cur.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        conn.commit()
    finally:
        conn.close()


@app.route(f"{ADMIN_BASE}/setup-email", methods=["GET"])
@_admin_required
def setup_email_page():
    gmail_user = _get_setting("GMAIL_USER")
    report_to  = ("".join((_get_setting("REPORT_TO") or "").split())) or "reallgym.tokyo@gmail.com"
    saved = request.args.get("saved", "")

    err_msg = request.args.get("msg", "")
    if saved == "1":
        banner = '<div class="ok">✓ 保存しました！翌朝8時からメールが届きます。</div>'
    elif saved == "sent":
        banner = '<div class="ok">✅ テストメールを送信しました！受信ボックスをご確認ください。</div>'
    elif saved == "err":
        detail = f"<br><small style='font-weight:400'>{err_msg}</small>" if err_msg else ""
        banner = f'<div class="ng">❌ 送信に失敗しました{detail}</div>'
    else:
        banner = ""

    if gmail_user:
        test_btn = f'<form method="POST" action="{ADMIN_BASE}/send-test-report" style="margin-top:12px"><button class="btn test-btn" type="submit">📧 テストメールを今すぐ送信</button></form>'
    else:
        test_btn = '<p class="hint">※ 先に上のフォームを保存するとテスト送信ボタンが表示されます</p>'

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>メール設定 — ABダイエット</title>
<style>
  body{{margin:0;padding:20px;background:#F3F4F6;font-family:-apple-system,'Helvetica Neue',Arial,sans-serif}}
  .card{{max-width:480px;margin:0 auto;background:#fff;border-radius:16px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,0.08)}}
  h1{{font-size:18px;font-weight:900;color:#374151;margin:0 0 6px}}
  p{{font-size:13px;color:#6B7280;margin:0 0 20px}}
  label{{display:block;font-size:13px;font-weight:700;color:#374151;margin-bottom:4px}}
  input{{width:100%;padding:10px 12px;border:1.5px solid #E5E7EB;border-radius:10px;font-size:14px;box-sizing:border-box;margin-bottom:14px}}
  input:focus{{outline:none;border-color:#FF6B35}}
  .btn{{width:100%;padding:13px;background:linear-gradient(135deg,#FF6B35,#e55a25);color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer}}
  .test-btn{{background:linear-gradient(135deg,#6366F1,#4F46E5)}}
  .ok{{background:#D1FAE5;color:#065F46;border-radius:10px;padding:12px;font-size:14px;font-weight:700;margin-bottom:16px}}
  .ng{{background:#FEE2E2;color:#991B1B;border-radius:10px;padding:12px;font-size:14px;font-weight:700;margin-bottom:16px}}
  .tip{{background:#FFF5F0;border-radius:10px;padding:14px;font-size:12px;color:#6B7280;margin-top:16px}}
  .tip strong{{color:#FF6B35}}
  .hint{{color:#9CA3AF;font-size:13px;margin-top:12px}}
  a{{color:#FF6B35}}
</style></head>
<body>
<div class="card">
  <h1>📧 メールレポート設定</h1>
  <p>毎朝8時に送るレポートのGmail設定をここで行えます</p>
  {banner}
  <form method="POST" action="{ADMIN_BASE}/setup-email-save">
    <label>送信元 Gmail アドレス</label>
    <input type="email" name="gmail_user" value="{gmail_user}" placeholder="yourname@gmail.com" required>
    <label>Gmail アプリパスワード（16桁）</label>
    <input type="password" name="gmail_app_password" placeholder="xxxx xxxx xxxx xxxx" required>
    <label>送信先メールアドレス</label>
    <input type="email" name="report_to" value="{report_to}" required>
    <button class="btn" type="submit">💾 保存する</button>
  </form>
  {test_btn}
  <div class="tip">
    <strong>アプリパスワードの取得方法：</strong><br>
    1. <a href="https://myaccount.google.com/apppasswords" target="_blank">このリンク</a> を開く<br>
    2. アプリ名に「AB Diet」と入力 → 「作成」<br>
    3. 表示された16桁をコピーして上の欄に貼り付ける<br><br>
    ※ リンクが開かない場合は先に
    <a href="https://myaccount.google.com/signinoptions/two-step-verification" target="_blank">2段階認証</a>
    を有効にしてください
  </div>
</div>
</body></html>"""


@app.route(f"{ADMIN_BASE}/setup-email-save", methods=["POST"])
@_admin_required
def api_setup_email():
    gmail_user = (request.form.get("gmail_user") or "").strip()
    gmail_pass = (request.form.get("gmail_app_password") or "").strip()
    report_to  = (request.form.get("report_to") or "").strip()
    if not gmail_user or not gmail_pass or not report_to:
        return "入力が不足しています", 400
    _set_setting("GMAIL_USER", gmail_user)
    _set_setting("GMAIL_APP_PASSWORD", gmail_pass)
    _set_setting("REPORT_TO", report_to)
    from flask import redirect
    return redirect(f"{ADMIN_BASE}/setup-email?saved=1")


@app.route(f"{ADMIN_BASE}/send-test-report", methods=["POST"])
@_admin_required
def api_send_test_report():
    from flask import redirect
    from urllib.parse import quote
    try:
        _send_daily_report()
        return redirect(f"{ADMIN_BASE}/setup-email?saved=sent")
    except Exception as e:
        return redirect(f"{ADMIN_BASE}/setup-email?saved=err&msg={quote(str(e))}")


# バックアップ対象のテーブル一覧（全データ）
_BACKUP_TABLES = [
    "daily_b_count", "daily_weight", "daily_exercise", "daily_meals",
    "daily_diary", "user_profile", "staff_comments", "usage_log", "settings",
]


def build_db_backup_json():
    """全テーブルの中身をJSON文字列にまとめて返す（列名＋行データ）。
    スキーマ非依存でcursor.descriptionから列名を取得する。"""
    dump = {
        "generated_at": datetime.datetime.now(JST).isoformat(),
        "use_pg": USE_PG,
        "tables": {},
    }
    conn = _get_conn()
    try:
        cur = conn.cursor()
        for tbl in _BACKUP_TABLES:
            try:
                cur.execute(f"SELECT * FROM {tbl}")
                cols = [d[0] for d in cur.description]
                rows = [list(r) for r in cur.fetchall()]
                dump["tables"][tbl] = {"columns": cols, "rows": rows}
            except Exception as _e:
                dump["tables"][tbl] = {"error": str(_e)}
    finally:
        conn.close()
    return json.dumps(dump, ensure_ascii=False, default=str)


def _make_backup_attachment():
    """バックアップJSONをgzip圧縮してメール添付パーツを作る。(part, 行数合計) を返す。"""
    raw = build_db_backup_json().encode("utf-8")
    total_rows = 0
    try:
        total_rows = sum(len(t.get("rows", [])) for t in json.loads(raw)["tables"].values()
                         if isinstance(t, dict))
    except Exception:
        pass
    gz = gzip.compress(raw)
    fname = f"ab-diet-backup-{datetime.datetime.now(JST).strftime('%Y%m%d')}.json.gz"
    part = MIMEBase("application", "gzip")
    part.set_payload(gz)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
    return part, total_rows


def _send_daily_report():
    gmail_user = "".join((_get_setting("GMAIL_USER") or "").split())
    gmail_pass = "".join((_get_setting("GMAIL_APP_PASSWORD") or "").split())
    report_to  = ("".join((_get_setting("REPORT_TO") or "").split())) or "reallgym.tokyo@gmail.com"
    if not gmail_user or not gmail_pass or not report_to:
        raise ValueError(f"メール設定が未登録です。{ADMIN_BASE}/setup-email で設定してください。")

    target_date = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    html_body = _build_report_html(target_date)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(f"[ABダイエット] デイリーレポート {target_date}", "utf-8")
    msg["From"]    = gmail_user
    msg["To"]      = report_to
    msg.attach(MIMEText("ABダイエット デイリーレポートです。HTMLメールをご覧ください。", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(gmail_user, gmail_pass)
        smtp.sendmail(gmail_user, [report_to], msg.as_bytes())


def _send_backup_email():
    """全データのバックアップ(.json.gz)をメール添付で送る。復元用の命綱。"""
    gmail_user = "".join((_get_setting("GMAIL_USER") or "").split())
    gmail_pass = "".join((_get_setting("GMAIL_APP_PASSWORD") or "").split())
    report_to  = ("".join((_get_setting("REPORT_TO") or "").split())) or "reallgym.tokyo@gmail.com"
    if not gmail_user or not gmail_pass or not report_to:
        print("[BACKUP][WARN] メール未設定のためバックアップを送れません")
        return
    part, total_rows = _make_backup_attachment()
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    msg = MIMEMultipart("mixed")
    msg["Subject"] = Header(f"[ABダイエット] データバックアップ {today}（{total_rows}件）", "utf-8")
    msg["From"]    = gmail_user
    msg["To"]      = report_to
    note = ("<p>ABダイエットの全データのバックアップです。<br>"
            "<b>このメールは削除せず保管してください。</b>万一またデータが消えても、"
            "この添付ファイルから復元できます。</p>")
    msg.attach(MIMEText(note, "html", "utf-8"))
    msg.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(gmail_user, gmail_pass)
        smtp.sendmail(gmail_user, [report_to], msg.as_bytes())
    print(f"[BACKUP] バックアップメールを送信しました（{total_rows}件）")


def _maybe_send_backup(force=False):
    """1日1回だけバックアップメールを送る（本番DB接続時のみ）。
    settingsに最終送信日を記録し、再起動が続いても多重送信しない。"""
    if not USE_PG:
        return
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    if not force and _get_setting("LAST_BACKUP_DATE") == today:
        return
    try:
        _send_backup_email()
        _set_setting("LAST_BACKUP_DATE", today)
    except Exception as _e:
        print(f"[BACKUP][WARN] バックアップ送信に失敗: {_e}")


@app.route(f"{ADMIN_BASE}/backup", methods=["GET"])
@_admin_required
def download_backup():
    """管理者が今すぐ全データのバックアップ(.json.gz)をダウンロードする。"""
    raw = build_db_backup_json().encode("utf-8")
    gz = gzip.compress(raw)
    fname = f"ab-diet-backup-{datetime.datetime.now(JST).strftime('%Y%m%d-%H%M')}.json.gz"
    return Response(
        gz,
        mimetype="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.route(f"{ADMIN_BASE}/restore", methods=["GET"])
@_admin_required
def restore_page():
    """ブラウザからバックアップファイルをアップロードして復元する画面（コマンド操作不要）。"""
    html = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>データ復元</title>
<style>
 body{font-family:sans-serif;max-width:560px;margin:40px auto;padding:0 16px;line-height:1.7;color:#222}
 h1{font-size:20px}.card{border:1px solid #ddd;border-radius:12px;padding:20px;margin-top:16px}
 input[type=file]{margin:12px 0}
 button{background:#FF6B35;color:#fff;border:0;border-radius:8px;padding:12px 20px;font-size:16px;font-weight:700}
 .note{color:#6B7280;font-size:13px}#result{white-space:pre-wrap;background:#f6f6f6;border-radius:8px;padding:12px;margin-top:12px;display:none}
</style></head><body>
<h1>📥 データ復元</h1>
<p class="note">バックアップファイル（ab-diet-backup-*.json.gz）を選んで「復元する」を押してください。
既存のデータは上書きされず、足りない分だけ復元されます。</p>
<div class="card">
  <form id="f">
    <input type="file" id="file" accept=".gz,.json" required><br>
    <button type="submit">復元する</button>
  </form>
  <div id="result"></div>
</div>
<script>
document.getElementById('f').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const fi=document.getElementById('file'); const r=document.getElementById('result');
  if(!fi.files[0]){return;}
  r.style.display='block'; r.textContent='復元中…';
  const fd=new FormData(); fd.append('file', fi.files[0]);
  try{
    const res=await fetch('restore-backup',{method:'POST',body:fd});
    const j=await res.json();
    r.textContent = res.ok ? ('✅ 復元しました\\n'+JSON.stringify(j.restored,null,2)) : ('⚠️ '+(j.error||'失敗'));
  }catch(err){ r.textContent='⚠️ 通信エラー: '+err; }
});
</script></body></html>"""
    return Response(html, mimetype="text/html")


@app.route(f"{ADMIN_BASE}/restore-backup", methods=["POST"])
@_admin_required
def restore_backup():
    """バックアップ(.json.gz か 生JSON)を受け取り、全テーブルへ復元する。
    既存行は上書きしない（ON CONFLICT DO NOTHING）。PostgreSQL接続時のみ。"""
    if not USE_PG:
        return jsonify({"error": "PostgreSQLに接続していないため復元できません"}), 400
    f = request.files.get("file")
    data = f.read() if f else request.get_data()
    try:
        raw = gzip.decompress(data)
    except Exception:
        raw = data
    try:
        dump = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return jsonify({"error": f"バックアップの読み込みに失敗: {e}"}), 400

    result = {}
    conn = _get_conn()
    try:
        cur = conn.cursor()
        for tbl, tdata in (dump.get("tables") or {}).items():
            if tbl not in _BACKUP_TABLES or not isinstance(tdata, dict) or "columns" not in tdata:
                continue
            cols = tdata["columns"]
            rows = tdata.get("rows", [])
            idx = [i for i, c in enumerate(cols) if c != "id"]  # id列はDB側で採番
            use_cols = [cols[i] for i in idx]
            if not use_cols:
                continue
            collist = ",".join(use_cols)
            placeholders = ",".join(["%s"] * len(use_cols))
            inserted = 0
            for row in rows:
                try:
                    cur.execute(
                        f"INSERT INTO {tbl} ({collist}) VALUES ({placeholders}) "
                        f"ON CONFLICT DO NOTHING",
                        [row[i] for i in idx],
                    )
                    if cur.rowcount and cur.rowcount > 0:
                        inserted += cur.rowcount
                except Exception:
                    pass
            result[tbl] = {"rows_in_backup": len(rows), "inserted": inserted}
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "restored": result})


@app.route("/api/setup", methods=["POST"])
@_admin_required
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
    # キャラカレンダー用：その日の進捗スコア（0〜100・任意）。未指定なら既存値を保持する。
    chara_score = (data or {}).get("chara_score")
    try:
        chara_score = float(chara_score) if chara_score is not None else None
    except (TypeError, ValueError):
        chara_score = None
    if not uid or not date:
        return jsonify({"error": "パラメータ不足"}), 400

    ts = datetime.datetime.now(JST).isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            if USE_PG:
                cur.execute(
                    """INSERT INTO daily_b_count (user_id, date, b_count, chara_score, created_at)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (user_id, date) DO UPDATE
                       SET b_count=%s,
                           chara_score=COALESCE(EXCLUDED.chara_score, daily_b_count.chara_score),
                           created_at=%s""",
                    (uid, date, b_count, chara_score, ts, b_count, ts)
                )
            else:
                cur.execute(
                    """INSERT INTO daily_b_count (user_id, date, b_count, chara_score, created_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, date) DO UPDATE
                       SET b_count=excluded.b_count,
                           chara_score=COALESCE(excluded.chara_score, daily_b_count.chara_score),
                           created_at=excluded.created_at""",
                    (uid, date, b_count, chara_score, ts)
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


@app.route("/api/daily-diary", methods=["POST"])
def post_daily_diary():
    """その日のひとこと日記を保存（50字まで）。空文字で削除。"""
    data = request.get_json(silent=True) or {}
    uid   = (data.get("user_id") or "").strip()
    date  = (data.get("date") or "").strip()
    diary = (data.get("diary") or "").strip()[:50]
    if not uid or not date:
        return jsonify({"error": "パラメータ不足"}), 400

    ts = datetime.datetime.now(JST).isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            if not diary:
                cur.execute(f"DELETE FROM daily_diary WHERE user_id={PH} AND date={PH}", (uid, date))
            elif USE_PG:
                cur.execute(
                    """INSERT INTO daily_diary (user_id, date, diary, created_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, date) DO UPDATE SET diary=%s, created_at=%s""",
                    (uid, date, diary, ts, diary, ts)
                )
            else:
                cur.execute(
                    """INSERT INTO daily_diary (user_id, date, diary, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(user_id, date) DO UPDATE SET diary=excluded.diary, created_at=excluded.created_at""",
                    (uid, date, diary, ts)
                )
            conn.commit()
        finally:
            conn.close()
    return jsonify({"ok": True})


@app.route("/api/daily-exercise", methods=["POST"])
def post_daily_exercise():
    """その日の運動によるBカウント消費合計を記録（運動の保存・削除時に呼ばれる）"""
    data = request.get_json()
    uid        = (data or {}).get("user_id", "").strip()
    ex_b_count = (data or {}).get("ex_b_count", 0)
    date       = (data or {}).get("date", "")   # YYYY-MM-DD (JST)
    try:
        ex_b_count = float(ex_b_count)
    except (TypeError, ValueError):
        ex_b_count = 0
    if not uid or not date:
        return jsonify({"error": "パラメータ不足"}), 400

    ts = datetime.datetime.now(JST).isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            if USE_PG:
                cur.execute(
                    """INSERT INTO daily_exercise (user_id, date, ex_b_count, created_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, date) DO UPDATE SET ex_b_count=%s, created_at=%s""",
                    (uid, date, ex_b_count, ts, ex_b_count, ts)
                )
            else:
                cur.execute(
                    """INSERT INTO daily_exercise (user_id, date, ex_b_count, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(user_id, date) DO UPDATE SET ex_b_count=excluded.ex_b_count, created_at=excluded.created_at""",
                    (uid, date, ex_b_count, ts)
                )
            conn.commit()
        finally:
            conn.close()
    return jsonify({"ok": True})


@app.route("/api/weekly-b")
@app.route("/api/monthly-b")
def get_monthly_b():
    """Bカウント履歴＋体重の全期間を返す（履歴はずっと保存）"""
    uid = request.args.get("user_id", "")
    if not uid:
        return jsonify({"total": 0, "days_count": 0, "daily": [], "date_start": "", "date_end": ""})

    # 起動時にPostgreSQLへ繋がっていなかった場合の復元（全ユーザー分の重い処理）は
    # リクエストをブロックせず、バックグラウンドで一度だけ実行する。
    # （同期実行すると、ワーカー再起動直後の初回履歴表示が数秒固まる原因になる）
    global _backfill_started
    if USE_PG and not _backfill_done and not _backfill_started:
        _backfill_started = True
        threading.Thread(target=backfill_bcount_from_meals, daemon=True).start()

    now = datetime.datetime.now(JST)
    date_end = now.strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT date, b_count, chara_score FROM daily_b_count
               WHERE user_id={PH} ORDER BY date""",
            (uid,)
        )
        b_rows = cur.fetchall()

        cur.execute(
            f"""SELECT date, weight FROM daily_weight
               WHERE user_id={PH} ORDER BY date""",
            (uid,)
        )
        w_rows = cur.fetchall()

        cur.execute(
            f"""SELECT date, ex_b_count FROM daily_exercise
               WHERE user_id={PH} ORDER BY date""",
            (uid,)
        )
        e_rows = cur.fetchall()

        cur.execute(
            f"""SELECT date, diary FROM daily_diary
               WHERE user_id={PH} ORDER BY date""",
            (uid,)
        )
        d_rows = cur.fetchall()
    finally:
        conn.close()
    date_start = min([r[0] for r in b_rows] + [r[0] for r in w_rows], default=date_end)

    b_by_date = {r[0]: r[1] for r in b_rows}
    c_by_date = {r[0]: r[2] for r in b_rows if r[2] is not None}
    w_by_date = {r[0]: r[1] for r in w_rows}
    e_by_date = {r[0]: r[1] for r in e_rows}
    d_by_date = {r[0]: r[1] for r in d_rows}

    total      = sum(b_by_date.values())
    days_count = len(b_by_date)
    all_dates  = sorted(set(b_by_date) | set(w_by_date) | set(e_by_date) | set(d_by_date))
    daily      = [
        {"date": d, "b_count": b_by_date.get(d), "weight": w_by_date.get(d),
         "ex_b_count": e_by_date.get(d), "diary": d_by_date.get(d),
         "chara_score": c_by_date.get(d)}
        for d in all_dates
    ]
    return jsonify({
        "total":      total,
        "days_count": days_count,
        "daily":      daily,
        "date_start": date_start,
        "date_end":   date_end,
    })


# 食事の明細＋写真サムネを日別に保存（過去5日分のみ保持し、それ以前は自動削除。
# ただしVIP会員（user_profile.is_vip）はスタッフ確認用に無期限保持する）
MEALS_RETAIN_DAYS = 5


@app.route("/api/daily-meals", methods=["POST"])
def post_daily_meals():
    data = request.get_json(silent=True) or {}
    uid     = (data.get("user_id") or "").strip()
    date    = (data.get("date") or "").strip()
    payload = data.get("payload")
    if not uid or not date or payload is None:
        return jsonify({"error": "パラメータ不足"}), 400
    payload_str = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    ts = datetime.datetime.now(JST).isoformat()
    cutoff = (datetime.datetime.now(JST) - datetime.timedelta(days=MEALS_RETAIN_DAYS)).strftime("%Y-%m-%d")
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            if USE_PG:
                cur.execute(
                    """INSERT INTO daily_meals (user_id, date, payload, created_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, date) DO UPDATE SET payload=%s, created_at=%s""",
                    (uid, date, payload_str, ts, payload_str, ts)
                )
            else:
                cur.execute(
                    """INSERT INTO daily_meals (user_id, date, payload, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(user_id, date) DO UPDATE SET payload=excluded.payload, created_at=excluded.created_at""",
                    (uid, date, payload_str, ts)
                )
            cur.execute(f"SELECT is_vip FROM user_profile WHERE user_id={PH}", (uid,))
            row = cur.fetchone()
            is_vip = bool(row and row[0])
            if not is_vip:
                cur.execute(f"DELETE FROM daily_meals WHERE user_id={PH} AND date < {PH}", (uid, cutoff))
            conn.commit()
        finally:
            conn.close()
    return jsonify({"ok": True})


@app.route("/api/daily-meals")
def get_daily_meals():
    uid  = request.args.get("user_id", "")
    date = request.args.get("date", "")
    if not uid or not date:
        return jsonify({"payload": None})
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT payload, created_at FROM daily_meals WHERE user_id={PH} AND date={PH}", (uid, date))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"payload": None})
    try:
        return jsonify({"payload": json.loads(row[0]), "updated_at": row[1]})
    except Exception:
        return jsonify({"payload": None})


# 事前チェック用プロンプト：栄養計算はせず「確認質問が必要か」だけを判定する
PRECHECK_PROMPT = """あなたは食事写真の事前チェック係です。栄養計算はせず、以下だけを判定して必ずJSONのみで回答してください（説明文・コードブロック不要）：
{
  "staple": { "present": trueまたはfalse, "food_name": "主食名（例：白米・パスタ・食パン）", "fixed_portion": trueまたはfalse },
  "oil": { "possible": trueまたはfalse }
}
- staple.present：白米・ご飯・丼もののご飯・チャーハン・カレーのご飯・パン・麺類などの主食が写っているか。
- staple.fixed_portion：量が確定できる場合のみtrue。おにぎり1個・食パン1枚など個数で分かる／栄養成分表示が写っている／主食が明らかに少量（100g未満）の場合。丼・皿盛りのご飯や麺は具や器で量が隠れるため必ずfalse。
- oil.possible：炒め物・揚げ物・アヒージョ・オイル系ドレッシングなど、調理油を大さじ0.5以上使っていそうか。
"""


@app.route("/precheck", methods=["POST"])
def precheck():
    """本解析の前に「大盛り・油の確認質問が必要か」だけを高速・低コストで判定する。
    ここで質問→回答を本解析のnote（補足）に含めることで、フル解析が1回で済む。"""
    client = get_client()
    if client is None:
        return jsonify({"error": "APIキーが設定されていません。"}), 401
    if "image" not in request.files:
        return jsonify({"error": "画像が見つかりません"}), 400

    file = request.files["image"]
    image_data = file.read()
    media_type = file.content_type
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media_type = "image/jpeg"
    base64_image, media_type = prepare_image_for_api(image_data, media_type)

    try:
        result = create_and_parse(
            client,
            model="claude-haiku-4-5-20251001",   # 事前チェックは高速・低コストのHaiku
            max_tokens=300,
            system=[{"type": "text", "text": PRECHECK_PROMPT}],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64_image}},
                    {"type": "text", "text": "この写真を判定してください。"},
                ],
            }],
        )
        return jsonify(result)
    except Exception as e:
        # 事前チェックの失敗は致命的ではない（本解析後の確認でフォローされる）
        return jsonify({"error": f"事前チェックに失敗しました: {e}"}), 502


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

    # 利用ログ記録（DB障害があっても解析は止めない）
    uid = request.form.get("user_id", "")
    _log_usage(uid)
    # 目的別レポート用に性別・目標を保存（毎回のリクエストに付いてくる）
    _save_user_goal(uid, request.form.get("gender", ""), request.form.get("goal", ""))

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
    # 写真への補足（任意）：食材名・量などユーザーの申告を最優先で反映する
    # （事前チェックで確認した大盛り・油の量の回答もここに含まれて届く）
    note = (request.form.get("note") or "").strip()
    if note:
        user_content.append({
            "type": "text",
            "text": ("━━━━━━━━━━━━━━━━━━━━━━━\n"
                     "【ユーザーからの補足情報（写真より最優先で反映すること）】\n"
                     f"{note[:1000]}\n"
                     "写真から読み取れない・写真と矛盾する場合も、この補足の食材名・量を正として判定する。\n"
                     "━━━━━━━━━━━━━━━━━━━━━━━"),
        })
    adv = _advice_context(request.form.get("gender", ""), request.form.get("goal", ""))
    if adv:
        user_content.append({"type": "text", "text": adv})

    try:
        result = create_and_parse(
            client,
            model="claude-sonnet-4-6",   # 通常解析は精度重視でSonnet
            max_tokens=16000,
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
        return jsonify(result)

    except EmptyAIResponse as e:
        return jsonify({"error": f"解析結果を取得できませんでした（{e.detail}）。もう一度お試しください。"}), 502
    except json.JSONDecodeError as e:
        return jsonify({"error": f"分析結果の解析に失敗しました。写真をもう一度撮り直してお試しください。({e})"}), 500
    except anthropic.AuthenticationError:
        return jsonify({"error": "APIキーが無効です。設定画面で正しいキーを入力してください。"}), 401
    except (anthropic.APITimeoutError, anthropic.APIConnectionError):
        return jsonify({"error": "AIサーバーが混み合っているようです。少し時間をおいて、もう一度お試しください。"}), 503
    except anthropic.APIError as e:
        return jsonify({"error": f"AI分析中にエラーが発生しました: {e}"}), 500
    except Exception as e:
        # 想定外の例外でもHTMLの500を返さない（フロントの res.json() が壊れ「通信エラー」に
        # なるのを防ぐ）。必ずJSONで返し、原因はログに残す。
        print(f"[ANALYZE][ERROR] {type(e).__name__}: {e}")
        return jsonify({"error": "画像の解析中に予期しないエラーが発生しました。もう一度お試しください。"}), 500


@app.route("/reanalyze", methods=["POST"])
def reanalyze():
    client = get_client()
    if client is None:
        return jsonify({"error": "APIキーが設定されていません。"}), 401

    # 画像は任意。文章入力（写真なし）の食事も再計算できるようにする。
    file = request.files.get("image")
    correction = request.form.get("correction", "").strip()
    if not correction:
        return jsonify({"error": "補足情報を入力してください"}), 400

    # 目的別レポート用に性別・目標を保存
    _save_user_goal(request.form.get("user_id", ""), request.form.get("gender", ""), request.form.get("goal", ""))

    base64_image = None
    media_type = None
    if file is not None and file.filename:
        image_data = file.read()
        media_type = file.content_type
        if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            media_type = "image/jpeg"
        base64_image, media_type = prepare_image_for_api(image_data, media_type)

    # 写真なし（文章入力）の再計算では、元の入力文を文脈として渡す
    original_text = (request.form.get("text", "") or "").strip()

    # 前回の解析結果（任意）。ある場合は「指摘された箇所だけ」を直し、他はそのまま維持する。
    previous_raw = (request.form.get("previous", "") or "").strip()
    previous_json = None
    if previous_raw:
        try:
            previous_json = json.loads(previous_raw)
        except Exception:
            previous_json = None

    # 写真がある場合は「写真とABダイエットルール」、ない場合（文章入力）は「入力文とABダイエットルール」を基準にさせる
    basis = "写真とABダイエットルール" if base64_image else "入力内容とABダイエットルール"
    text_context = ""
    if not base64_image and original_text:
        text_context = f"\n【元の入力内容（写真なし・文章入力）】\n{original_text}\n"

    if previous_json is not None:
        correction_text = f"""━━━━━━━━━━━━━━━━━━━━━━━
【前回の解析結果（このJSONをベースにする）】
{json.dumps(previous_json, ensure_ascii=False)}
{text_context}
【ユーザーからの訂正情報】
{correction}

上記「前回の解析結果」をベースに、ユーザーが訂正した箇所だけを修正してください。
- 訂正が指している食材（foods配列内の該当項目）のみ、{basis}に基づいて計算し直す。
- それ以外の食材は、name・category・kcal_per_serving・amount・b_count・protein_g・veg_g・reason をすべて前回のまま一切変更しないこと。
- 訂正で食材の種類が変わる場合（例：豆腐→ヨーグルト）は、その項目のname・category・kcal・b_count等を新しい食材に合わせて計算し直す。
- 訂正が新しい食材の追加や削除を指す場合のみ、該当項目を追加・削除してよい。訂正に関係ない食材は増やしも減らしもしない。
- 変更後、total_b_count・total_protein_g・total_veg_g を foods の合計に合わせて必ず計算し直す。
- advice は訂正内容を反映して簡潔に更新する。
【重要】前置きや説明文は一切書かず、システムプロンプト指定のJSONだけを出力すること。
━━━━━━━━━━━━━━━━━━━━━━━"""
    else:
        correction_text = f"""━━━━━━━━━━━━━━━━━━━━━━━
{text_context}【ユーザーからの補足・訂正情報】
{correction}

この補足情報を最優先して、ABダイエットルールに基づき再計算してください。
【重要】前置きや説明文は一切書かず、システムプロンプト指定のJSONだけを出力すること。
━━━━━━━━━━━━━━━━━━━━━━━"""

    reanalyze_content = []
    if base64_image:
        reanalyze_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64_image,
            },
        })
    reanalyze_content.append({
        "type": "text",
        "text": correction_text,
    })
    adv = _advice_context(request.form.get("gender", ""), request.form.get("goal", ""))
    if adv:
        reanalyze_content.append({"type": "text", "text": adv})

    try:
        result = create_and_parse(
            client,
            model="claude-sonnet-4-6",   # 再計算（修正時）は精度重視でSonnet
            max_tokens=16000,
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
        return jsonify(result)

    except EmptyAIResponse as e:
        return jsonify({"error": f"再計算の結果を取得できませんでした（{e.detail}）。もう一度お試しください。"}), 502
    except json.JSONDecodeError as e:
        return jsonify({"error": f"分析結果の解析に失敗しました。({e})"}), 500
    except anthropic.AuthenticationError:
        return jsonify({"error": "APIキーが無効です。"}), 401
    except (anthropic.APITimeoutError, anthropic.APIConnectionError):
        return jsonify({"error": "AIサーバーが混み合っているようです。少し時間をおいて、もう一度お試しください。"}), 503
    except anthropic.APIError as e:
        return jsonify({"error": f"AI分析中にエラーが発生しました: {e}"}), 500
    except Exception as e:
        print(f"[REANALYZE][ERROR] {type(e).__name__}: {e}")
        return jsonify({"error": "再計算中に予期しないエラーが発生しました。もう一度お試しください。"}), 500


@app.route("/analyze-text", methods=["POST"])
def analyze_text():
    """写真を撮り忘れたとき用：食事内容を文章で受け取り、Bカウントを推定する。"""
    client = get_client()
    if client is None:
        return jsonify({"error": "APIキーが設定されていません。設定画面から登録してください。"}), 401

    text = (request.form.get("text", "") or "").strip()
    if not text:
        return jsonify({"error": "食べた内容を入力してください"}), 400

    # 利用ログ記録（写真分析と同じく1回としてカウント。DB障害があっても解析は止めない）
    uid = request.form.get("user_id", "")
    _log_usage(uid)
    # 目的別レポート用に性別・目標を保存
    _save_user_goal(uid, request.form.get("gender", ""), request.form.get("goal", ""))

    text_prompt = f"""━━━━━━━━━━━━━━━━━━━━━━━
【テキスト入力モード・写真なし】
ユーザーは食事の写真を撮り忘れたため、食べた内容を文章で入力しました。
以下の説明から食材と量を推定し、システムプロンプトのABダイエットのルールに従ってBカウントを判定してください。
量が明記されていない場合は、一般的な1人前として推定してください。

【食べたもの（ユーザー入力）】
{text}

写真がないため推定が大まかになる旨を、advice欄の最後に一言添えてください。
出力はシステムプロンプト指定の通常のJSON（各foodにprotein_g・veg_gを含め、
total_b_count / total_protein_g / total_veg_g / advice も入れる）で回答してください。
━━━━━━━━━━━━━━━━━━━━━━━"""

    text_content = [{"type": "text", "text": text_prompt}]
    adv = _advice_context(request.form.get("gender", ""), request.form.get("goal", ""))
    if adv:
        text_content.append({"type": "text", "text": adv})

    try:
        result = create_and_parse(
            client,
            model="claude-sonnet-4-6",   # 文章入力も精度重視でSonnet
            max_tokens=16000,
            system=[
                {
                    "type": "text",
                    "text": ANALYSIS_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": text_content}],
        )
        return jsonify(result)

    except EmptyAIResponse as e:
        return jsonify({"error": f"解析結果を取得できませんでした（{e.detail}）。もう一度お試しください。"}), 502
    except json.JSONDecodeError as e:
        return jsonify({"error": f"分析結果の解析に失敗しました。もう一度お試しください。({e})"}), 500
    except anthropic.AuthenticationError:
        return jsonify({"error": "APIキーが無効です。設定画面で正しいキーを入力してください。"}), 401
    except (anthropic.APITimeoutError, anthropic.APIConnectionError):
        return jsonify({"error": "AIサーバーが混み合っているようです。少し時間をおいて、もう一度お試しください。"}), 503
    except anthropic.APIError as e:
        return jsonify({"error": f"AI分析中にエラーが発生しました: {e}"}), 500
    except Exception as e:
        print(f"[ANALYZE-TEXT][ERROR] {type(e).__name__}: {e}")
        return jsonify({"error": "解析中に予期しないエラーが発生しました。もう一度お試しください。"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
