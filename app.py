import os
import base64
import json
from flask import Flask, render_template, request, jsonify
import anthropic
from dotenv import load_dotenv, set_key

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH, override=True)
app = Flask(__name__)

ANALYSIS_PROMPT = """この写真に写っている食事・食材をすべて特定し、ABダイエットのルールに基づいてBカウントを計算してください。

【ABダイエット ルール】

■ 基本分類
- B食材：1人前あたりの熱量が120kcal以上のもの
- A食材：1人前あたりの熱量が120kcal未満のもの（いくら食べてもBカウントなし）

■ 原則としてA食材扱いするもの（基本的に120kcal未満）
- 野菜全般（調理法問わず）
- 果物全般
- 魚介類（下記の脂の多い魚種を除く）
- 鶏肉（下記の例外を除く）
- プロテインパウダー単体
- きのこ・海藻・豆腐・こんにゃく・お茶・ブラックコーヒーなど

■ 例外：以下はB食材として扱う
【鶏肉のB食材例外】
- 皮付きの鶏肉（もも肉皮付き、手羽元、手羽先、チキンスキンなど）
- 1人前で120kcalを超えると判断される量の鶏肉

【脂の多い魚（B食材固定）】
- サバ、サーモン、ブリ、サンマ、ウナギ
  → これらは1人前で必ず120kcalを超えるためB食材

■ 牛乳の特別ルール
- 牛乳はB食材
- 1人前の定義：コップ1杯 = 400ml
- 200mlの場合：0.5カウント
- 量に応じて比例計算（例：100ml → 0.25カウント）
- プロテインを牛乳で割った場合：プロテイン自体はA食材、牛乳分のみBカウント

■ Bカウント計算
- 極少量（小さじ1杯程度・飾り程度）：0カウント
- 半人前程度：0.5カウント
- 1人前（一般的な量）：1カウント
- 大盛り・2人前程度：2カウント
- 牛乳は400mlを1カウントとして量で比例計算

必ず以下のJSON形式のみで回答してください。JSONの前後に説明文やコードブロックは不要です：
{
  "foods": [
    {
      "name": "食材・料理名",
      "category": "A",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量の説明（例：1人前、半人前、大盛りなど）",
      "b_count": 0,
      "reason": "A食材のため（1人前約○kcal）"
    },
    {
      "name": "食材・料理名",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量の説明",
      "b_count": 1,
      "reason": "B食材（1人前約○kcal）、1人前のためBカウント1"
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
