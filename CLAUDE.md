# ABダイエットカウンター 開発メモ

## 反映の進め方（オーナーの依頼で「全部お任せ」）

オーナーから変更の指示を受けたら、確認を挟まず最後まで一気に反映する：

1. 変更を実装する
2. 動作確認（バックエンドのテスト＋Playwrightの実ブラウザ確認）を**必ず**行う
3. 作業ブランチ `claude/photo-caption-supplement-w36v8p` にコミット＆プッシュ
4. `main` へのPRを作成し、そのままマージする（オーナーの手動マージは不要）
5. Render が `main` から自動デプロイ → 数分でアプリに反映
6. 完了後に「反映しました」と報告する

- 見た目・文言・機能追加・修正はそのまま反映してよい。
- 例外：ユーザーデータの削除など「後戻りしにくい破壊的変更」だけは反映前に一度確認する。
- 反映が終わっていない（＝mainにマージされていない）変更は「まだ公開アプリに出ていない」状態なので、報告時に区別する。

## デプロイ構成

- ホスティング：Render（`render.yaml`）。`main` ブランチを自動デプロイ。
- 起動：`gunicorn app:app`／ビルド：`pip install -r requirements.txt`
- 環境変数 `ANTHROPIC_API_KEY` が必要（Render側で設定）。

## アプリ構成

- `app.py` … Flask アプリ本体。主要エンドポイント：
  - `/analyze` … 写真から食事解析（Claude Vision）。任意の `note`（補足）に対応。
  - `/reanalyze` … 訂正による再計算。`previous`（前回結果）があれば**指摘箇所だけ**を修正し他は維持。
  - `/analyze-text` … 写真なし・文章だけの解析。
  - `ANALYSIS_PROMPT` … ABダイエット判定ルールのシステムプロンプト。
- `templates/index.html` … フロント一式（1ファイルにHTML/CSS/JSを内包）。
  - 状態は localStorage に保存し、サーバーにも日別同期。
  - アプリ内「アップデートお知らせ」は `NOTICE_KEY`（`ab-diet-notice-vNN`）を更新すると既存端末にも再表示される。

## ローカル動作確認

```bash
pip install -r requirements.txt
python3 app.py          # http://localhost:5001
```

- Playwright は `/opt/pw-browsers/chromium` を `executablePath` に指定して起動する（`playwright install` は不要）。
- `usage.db` はローカル実行で生成されるためコミットしない（`.gitignore` 済み）。
