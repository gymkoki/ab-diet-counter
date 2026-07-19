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

## アップデートお知らせ（重要・オーナー指示）

- **アプリを修正しても、アプリ内「アップデートお知らせ」は自動で出さない**。
- お知らせを出す（`index.html` の No.／本文と `NOTICE_KEY = 'ab-diet-notice-vNN'` を更新する）のは、
  **オーナーから明示的に「アップデートを知らせて」と指示があったときだけ**。
- 通常の修正では NOTICE_KEY や No. は触らない。

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

## 回帰テスト（必ず実行してからマージする）

- `tests/` に回帰テストがある（AIはモックするのでAPIキー不要・数秒で終わる）：

```bash
pip install pytest && python3 -m pytest tests/ -q
```

- GitHub Actions（`.github/workflows/test.yml`）でも全PR・main pushで自動実行される。
- 経緯：2026-07 に「文章で入力すると通信エラー」障害が発生。原因は、前面のままの
  通信断で `fetchAnalyze` が再送せず即エラーにしていたこと。対策として
  ①前面でも自動再送、②`/analyze-text` のAI試行を60秒/回に制限、
  ③通信エラーと画面側バグのメッセージを区別、④このテスト群を追加した。
- **フロントは1ファイル（index.html）に全JSが入っているため、構文エラー1つで
  アプリ全体が止まる**。テストが `node --check` で検知するので、マージ前に必ず通すこと。

## ローカル動作確認

```bash
pip install -r requirements.txt
python3 app.py          # http://localhost:5001
```

- Playwright は `/opt/pw-browsers/chromium` を `executablePath` に指定して起動する（`playwright install` は不要）。
- `usage.db` はローカル実行で生成されるためコミットしない（`.gitignore` 済み）。
