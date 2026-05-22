# ============================================================
#  GitHub 認証設定スクリプト（初回のみ実行）
# ============================================================

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  GitHub 認証設定" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "GitHubのユーザー名とPersonal Access Tokenを設定します。" -ForegroundColor White
Write-Host ""
Write-Host "★ Personal Access Token の取得方法:" -ForegroundColor Yellow
Write-Host "  1. https://github.com/settings/tokens を開く" -ForegroundColor White
Write-Host "  2. Generate new token (classic) をクリック" -ForegroundColor White
Write-Host "  3. Note に ab-diet-windows と入力" -ForegroundColor White
Write-Host "  4. Expiration を No expiration に設定" -ForegroundColor White
Write-Host "  5. repo にチェックを入れる" -ForegroundColor White
Write-Host "  6. Generate token をクリック" -ForegroundColor White
Write-Host "  7. 表示された ghp_... をコピー" -ForegroundColor White
Write-Host ""

$username = Read-Host "GitHubのユーザー名を入力してください"
$token    = Read-Host "Personal Access Token を入力してください"

git config --global user.name  $username
git config --global user.email "$username@users.noreply.github.com"
git config --global credential.helper manager-core

cmdkey /add:LegacyGeneric:target=git:https://github.com /user:$username /pass:$token

Write-Host ""
Write-Host "認証情報を保存しました。" -ForegroundColor Green
Write-Host "設定完了！ git push が使えるようになりました。" -ForegroundColor Green
Write-Host ""
Read-Host "Enterキーで閉じる"
