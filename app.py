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

■ 食材の3分類
- A食材：1人前あたりの熱量が120kcal未満 → Bカウントなし（いくら食べてもOK）
- グッドB食材：1人前あたりの熱量が120kcal以上200kcal未満 → 1人前につき0.5カウント
- B食材：1人前あたりの熱量が200kcal以上 → 1人前につき1カウント

■ 原則としてA食材扱いするもの（基本的に120kcal未満）
- 野菜全般（調理法問わず）
- 果物全般
- 魚介類（下記の脂の多い魚種を除く）
- 鶏肉（下記の例外を除く）
- プロテインパウダー単体
- きのこ・海藻・豆腐・こんにゃく・お茶・ブラックコーヒーなど

■ 例外：以下はB系食材として扱う
【鶏肉の例外】
- 皮付きの鶏肉（もも肉皮付き、手羽元、手羽先、チキンスキンなど）
- カロリーに応じてグッドBまたはBに分類する

【脂の多い魚】
- サバ、サーモン、ブリ、サンマ、ウナギ
  → カロリーに応じてグッドBまたはBに分類する

■ 牛乳の特別ルール
- 牛乳はB食材（1人前=400ml、約250kcal）
- 1人前につき1カウント
- 200mlの場合：0.5カウント（量に比例して計算）
- プロテインを牛乳で割った場合：プロテイン自体はA食材、牛乳分のみカウント

■ Bカウント計算（量による調整）― 量の判定を慎重に行うこと

【グッドB食材（120〜200kcal/人前）の量別カウント】
- 半人前未満（少量）：0カウント
- 半人前程度：0.25カウント
- 1人前：0.5カウント
- 大盛り・2人前：1カウント

【B食材（200kcal以上/人前）の量別カウント】
- 半人前未満（小さいサイズ・少量）：0カウント
- 半人前程度：0.5カウント
- 1人前（一般的な量）：1カウント
- 大盛り・2人前：2カウント

■ 揚げ物の分解カウントルール（重要）
揚げ物（唐揚げ・フライドチキン・天ぷら・フライ類）は、必ず以下の食材に分解してそれぞれBカウントを計算すること。

【揚げ物の構成要素】
1. 主食材（肉・魚・野菜など）
   - 鶏もも肉（皮付き）100g ≒ 200kcal以上 → B食材 → B1カウント
   - 鶏むね肉（皮なし）100g ≒ 120kcal未満 → A食材 → 0カウント
   ※1人前の量（目安100g）で判断する

2. 揚げ油（吸油分）
   - 揚げ物は調理時に食材重量の約20%の油を吸収する
   - 1人前（100g）の揚げ物 → 吸油量 ≒ 大さじ2杯（約20g・約180kcal）→ グッドB → 0.5カウント
   - ただし衣が厚い揚げ物（フライドチキン・カツなど）は吸油量が多く200kcal超 → B → 1カウント
   ※揚げ油は必ず独立した食材として計上すること

3. 衣（小麦粉・卵・パン粉など）
   - 薄い衣（唐揚げの薄衣）：1人前あたり約30〜50kcal → A食材 → 0カウント
   - 厚い衣（パン粉・フライ衣）：1人前あたり約80〜120kcal → A食材またはグッドB → 0〜0.5カウント
   ※1人前の衣の量が1人前分（単体で食べる量）に達しないためノーカウントが基本

【具体例】
- フライドチキン（骨付き・皮付き・衣付き、4〜5個）
  → 鶏もも肉（皮付き）1人前（約200kcal） → B1カウント
  → 吸油（厚衣・200kcal超） → B1カウント
  → 衣（小麦粉・卵）1人前未満 → 0カウント
  → 合計 B2カウント

- 鶏むね肉の唐揚げ（皮なし・薄衣、4〜5個）
  → 鶏むね肉（皮なし）1人前（約120kcal未満） → A食材 → 0カウント
  → 吸油（薄衣・約180kcal） → グッドB → 0.5カウント
  → 合計 B0.5カウント

■ 量の判定における注意事項（重要）
- 「1人前」とは、一般的な飲食店や市販品の標準サイズ・標準量を基準とする
- 小さなお菓子・焼き菓子（フィナンシェ・マドレーヌ・クッキー1〜2枚など）は、
  仮にB食材であっても1個あたりの実際のカロリーで判断する
  → 例：フィナンシェ1個≒80〜100kcal → グッドB未満 → A食材
  → 例：小さいクッキー2枚≒100〜150kcal → グッドB相当 → 0.5カウント以下
- 「B食材の種類である」ことと「1人前以上ある」ことは別問題
  実際の量・個数・サイズから写真のカロリーを推定し、カウントを決める
- 過大評価より過小評価を優先すること（迷ったら少なめのカウントを採用）

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
      "category": "グッドB",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量の説明",
      "b_count": 0.5,
      "reason": "グッドB食材（1人前約○kcal、120〜200kcal）、1人前のためBカウント0.5"
    },
    {
      "name": "食材・料理名",
      "category": "B",
      "kcal_per_serving": 1人前の推定kcal（整数）,
      "amount": "写真での量の説明",
      "b_count": 1,
      "reason": "B食材（1人前約○kcal、200kcal以上）、1人前のためBカウント1"
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
