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

━━━━━━━━━━━━━━━━━━━━━━━
■ STEP1：食材カテゴリの判定（カロリーではなく種類で決まる）
━━━━━━━━━━━━━━━━━━━━━━━

【A食材】― どれだけ食べてもBカウント0
以下に該当する食材は量に関係なく常にA食材。
- 野菜全般（生・加熱・炒め・茹でいずれも）
- 果物全般
- 鶏肉（皮なし）― むね肉・ささみなど
- 白身魚・えび・いか・たこ・貝類などの低脂質魚介
- きのこ類・海藻類
- 豆腐・こんにゃく・納豆以外の大豆製品
- プロテインパウダー単体
- 調味料少量（黒胡椒・塩・醤油・酢・ハーブ・スパイスなど）
- お茶・ブラックコーヒー・水
- 手作りスムージー（上記A食材の素材のみ使用）

【グッドB食材】― 1人前あたり120〜200kcal未満の食材
以下が代表例（1人前の定義はカッコ内）。
- 納豆（1パック70kcal、1人前＝2パック140kcal）
- バナナ（1本約80〜100kcal、1人前＝1〜2本）
- オートミール（1人前30〜40g、約120〜150kcal）
- トウモロコシ（1本約150kcal）
- さつまいも（1人前100g、約130kcal）
- 皮付き鶏もも肉（1人前100g、約160kcal）
- 脂の多い魚（サバ・サーモン・ブリ・サンマ・ウナギ）― 1人前によりグッドBかBか判断
- 全粒粉パン1枚（約120〜140kcal）

【B食材】― 1人前あたり200kcal以上の食材
以下が代表例（1人前の定義はカッコ内）。
- 白米（1杯150g、約250kcal）
- パン（食パン2枚、約250kcal）
- 麺類（1人前、約270〜350kcal）
- 揚げ物（吸油込みで計算、後述）
- 油・バター（大さじ2以上、約200kcal超）
- チーズ（1人前50g以上、約200kcal超）
- スイーツ・お菓子（1人前で200kcal超のもの）
- アルコール飲料（1人前で200kcal超のもの）
- 牛乳（1人前400ml、約250kcal）

━━━━━━━━━━━━━━━━━━━━━━━
■ STEP2：量によるBカウント計算
━━━━━━━━━━━━━━━━━━━━━━━

【A食材のカウント】
→ 量に関係なく常に0カウント。大量に食べても0。

【グッドB食材のカウント】
写真に写っている実際の量のカロリーを推定して判定：
- 実際のカロリーが120kcal未満（少量）→ 0カウント
- 実際のカロリーが120〜200kcal未満（1人前相当）→ 0.5カウント
- 実際のカロリーが200kcal以上（2人前・大量）→ 1カウント

  例）納豆
  ・1パック（70kcal）→ 120kcal未満 → 0カウント
  ・2パック（140kcal）→ 120〜200kcal → 0.5カウント
  ・3パック（210kcal）→ 200kcal以上 → 1カウント

【B食材のカウント】
1人前＝1カウントを基準に、量で比例：
- 少量（例：油小さじ1、薄くぬったバターなど）→ 0カウント
- 半人前 → 0.5カウント
- 1人前（標準的な量）→ 1カウント
- 大盛り・2人前 → 2カウント

  例）白米
  ・少量（30g程度）→ 0カウント
  ・半膳（75g、約125kcal）→ 0.5カウント
  ・1杯（150g、約250kcal）→ 1カウント
  ・大盛り（300g超）→ 2カウント

  例）油
  ・小さじ1（約40kcal）→ 0カウント（少量）
  ・大さじ1（約110kcal）→ 0カウント（少量）
  ・大さじ1.5（約165kcal）→ 0.5カウント（半人前相当）
  ・大さじ2以上（約200kcal超）→ 1カウント（1人前）

━━━━━━━━━━━━━━━━━━━━━━━
■ 揚げ物の特別ルール（吸油込みで食材分類を決める）
━━━━━━━━━━━━━━━━━━━━━━━
揚げ物は「揚げた後の合計カロリー（吸油込み）」で食材分類を決める。
- 揚げ物は調理時に食材重量の約20%の油を吸収する
- 分類後はB食材のカウントルールを適用

  例）鶏むね肉唐揚げ 100g → 肉110kcal＋吸油180kcal ＝ 290kcal → B食材 → B1カウント
  例）フライドチキン（皮付き）100g → 肉200kcal＋吸油200kcal ＝ 400kcal → B食材 → B2カウント
  例）野菜天ぷら1〜2個 → 野菜20kcal＋吸油50kcal ＝ 70kcal → A食材 → 0カウント

━━━━━━━━━━━━━━━━━━━━━━━
■ 市販ドリンクのルール（500ml換算）
━━━━━━━━━━━━━━━━━━━━━━━
市販の飲み物（ジュース・乳飲料・炭酸飲料・スポーツドリンクなど）は
実際の容量を500ml換算してカテゴリを判定する。
- 500ml換算で120kcal未満 → A食材 → 0カウント
- 500ml換算で120〜200kcal未満 → グッドB → 0.5カウント
- 500ml換算で200kcal以上 → B食材 → 1カウント

  例）330mlで130kcal → 500ml換算197kcal → グッドB → 0.5カウント
  例）500mlで250kcal → B食材 → 1カウント

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
