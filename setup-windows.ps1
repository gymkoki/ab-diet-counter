# ============================================================
#  ABダイエット Bカウンター - Windows開発環境 自動セットアップ
# ============================================================
# PowerShell を「管理者として実行」してから使ってください

$ErrorActionPreference = "Stop"
$REPO_URL = "https://github.com/gymkoki/ab-diet-counter.git"
$REPO_DIR  = "$env:USERPROFILE\Desktop\ab-diet-app"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  ABダイエット 開発環境セットアップ" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. Git ──────────────────────────────────────────────────
Write-Host "[1/4] Git を確認中..." -ForegroundColor Yellow
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "  Git が見つかりません。インストールします..." -ForegroundColor Red
    try {
        winget install --id Git.Git -e --source winget --silent
        $env:PATH += ";C:\Program Files\Git\cmd"
        Write-Host "  Git のインストール完了" -ForegroundColor Green
    } catch {
        Write-Host ""
        Write-Host "  ★ winget が使えませんでした。" -ForegroundColor Red
        Write-Host "  以下のURLから Git を手動でインストールしてください:" -ForegroundColor Red
        Write-Host "  https://git-scm.com/download/win" -ForegroundColor White
        Write-Host "  インストール後、このスクリプトを再実行してください。" -ForegroundColor White
        Read-Host "  Enterキーで終了"
        exit 1
    }
} else {
    Write-Host "  Git OK: $(git --version)" -ForegroundColor Green
}

# ── 2. Node.js ──────────────────────────────────────────────
Write-Host "[2/4] Node.js を確認中..." -ForegroundColor Yellow
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "  Node.js が見つかりません。インストールします..." -ForegroundColor Red
    try {
        winget install --id OpenJS.NodeJS.LTS -e --source winget --silent
        $env:PATH += ";C:\Program Files\nodejs"
        Write-Host "  Node.js のインストール完了" -ForegroundColor Green
    } catch {
        Write-Host ""
        Write-Host "  ★ winget が使えませんでした。" -ForegroundColor Red
        Write-Host "  以下のURLから Node.js (LTS) を手動でインストールしてください:" -ForegroundColor Red
        Write-Host "  https://nodejs.org" -ForegroundColor White
        Write-Host "  インストール後、このスクリプトを再実行してください。" -ForegroundColor White
        Read-Host "  Enterキーで終了"
        exit 1
    }
} else {
    Write-Host "  Node.js OK: $(node --version)" -ForegroundColor Green
}

# ── 3. Claude Code ──────────────────────────────────────────
Write-Host "[3/4] Claude Code を確認中..." -ForegroundColor Yellow
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "  Claude Code をインストール中..." -ForegroundColor Red
    npm install -g @anthropic-ai/claude-code
    Write-Host "  Claude Code のインストール完了" -ForegroundColor Green
} else {
    Write-Host "  Claude Code OK" -ForegroundColor Green
}

# ── 4. リポジトリをクローン ──────────────────────────────────
Write-Host "[4/4] コードをダウンロード中..." -ForegroundColor Yellow
if (Test-Path $REPO_DIR) {
    Write-Host "  フォルダが既に存在します。最新に更新します..." -ForegroundColor Yellow
    Set-Location $REPO_DIR
    git pull origin main
} else {
    git clone $REPO_URL $REPO_DIR
    Set-Location $REPO_DIR
}
Write-Host "  コードの準備完了: $REPO_DIR" -ForegroundColor Green

# ── 完了メッセージ ────────────────────────────────────────────
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  セットアップ完了！" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "次回からの使い方:" -ForegroundColor White
Write-Host ""
Write-Host "  1. PowerShell を開く" -ForegroundColor White
Write-Host "  2. 以下のコマンドを実行:" -ForegroundColor White
Write-Host ""
Write-Host "     cd $REPO_DIR" -ForegroundColor Yellow
Write-Host "     claude" -ForegroundColor Yellow
Write-Host ""
Write-Host "  3. Claude Code が起動したら普通に話しかけてください" -ForegroundColor White
Write-Host ""

$open = Read-Host "今すぐ Claude Code を起動しますか？ (y/n)"
if ($open -eq "y" -or $open -eq "Y") {
    Set-Location $REPO_DIR
    claude
}
