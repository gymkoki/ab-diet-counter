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

■ ドリンク類のルール（重要）
市販の飲み物（ジュース・スムージー・乳飲料・炭酸飲料・スポーツドリンクなど）は
500mlを1人前として換算し、以下の基準で分類する。

【500ml換算カロリーでの分類】
- 500ml換算で120kcal未満  → A食材 → 0カウント
- 500ml換算で120〜200kcal未満 → グッドB → B0.5カウント
- 500ml換算で200kcal以上  → B食材 → B1カウント

【換算方法】
- 実際の容量が異なる場合は比例計算で500ml換算する
  例）330mlで130kcal → 500ml換算 ≒ 197kcal → グッドB → B0.5カウント
  例）500mlで250kcal → B食材 → B1カウント
  例）350mlで53kcal（無糖炭酸など） → 500ml換算 ≒ 76kcal → A食材 → 0カウント

【例外：以下はA食材扱い】
- 家でリアルフード（果物・野菜・プロテインなど）を使って手作りしたスムージー
  → 素材がA食材であれば、まとめてA食材としてカウント
- お茶・ブラックコーヒー・水 → カロリーほぼ0のためA食材

■ 牛乳の特別ルール
- 牛乳はドリンクルール対象外で従来通り
- 1人前=400ml、約250kcal → B食材 → B1カウント
- 200mlの場合：0.5カウント（量に比例して計算）
- プロテインを牛乳で割った場合：プロテイン自体はA食材、牛乳分のみカウント

■ Bカウント計算（写真に写っている実際の量のカロリーで判定）

食材ごとに「写真に写っている実際の量のカロリー」を推定し、以下の3段階のみで分類する：
- 120kcal未満 → A食材扱い → 0カウント
- 120kcal以上200kcal未満 → グッドB → 0.5カウント
- 200kcal以上 → B食材 → 1カウント

0.25カウントや1.5カウントなどの中間値は使用しない。
1食材あたりの実際の消費カロリーを推定し、上記3段階に当てはめるだけ。

【具体例】
- オリーブオイル（仕上げかけ・大さじ1.5）≒ 160kcal → グッドB → 0.5カウント
- オリーブオイル（少量・小さじ1以下）≒ 40kcal → 120kcal未満 → 0カウント
- ご飯1杯（150g）≒ 250kcal → B食材 → 1カウント
- ご飯半膳（75g）≒ 125kcal → グッドB → 0.5カウント
- チーズ（少量トッピング・5g程度）≒ 20kcal → 0カウント
- チーズ（1人前・30g程度）≒ 100kcal → 0カウント
- チーズ（たっぷり・50g超）≒ 180kcal → グッドB → 0.5カウント

■ 揚げ物のカロリー計算ルール（重要）
揚げ物（唐揚げ・フライドチキン・天ぷら・フライ類）は、調理後の実際のカロリー（吸油込み）で分類すること。

【揚げ油の吸収について】
- 揚げ物は調理時に食材重量の約20%の油を吸収する（20g ≒ 180kcal）
- 分類は「主食材の素のカロリー」ではなく「揚げた後の合計カロリー」で行う

【具体例】
- 鶏むね肉（皮なし）の唐揚げ 100g
  → 鶏むね肉 ≒ 110kcal ＋ 吸油 ≒ 180kcal ＝ 合計 ≒ 290kcal
  → 200kcal以上のためB食材 → B1カウント

- 鶏もも肉（皮付き）のフライドチキン 100g
  → 鶏もも肉 ≒ 200kcal ＋ 吸油（厚衣） ≒ 200kcal ＝ 合計 ≒ 400kcal
  → 200kcal以上かつ2人前相当のためB2カウント

- 野菜の天ぷら（1〜2個）
  → 野菜 ≒ 20kcal ＋ 吸油 ≒ 50kcal ＝ 合計 ≒ 70kcal
  → 120kcal未満のためA食材 → 0カウント

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
