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
Write-Host "  2. 「Generate new token (classic)」をクリック" -ForegroundColor White
Write-Host "  3. Note に「ab-diet-windows」と入力" -ForegroundColor White
Write-Host "  4. Expiration を「No expiration」に設定" -ForegroundColor White
Write-Host "  5. 「repo」にチェックを入れる" -ForegroundColor White
Write-Host "  6. 「Generate token」をクリック" -ForegroundColor White
Write-Host "  7. 表示されたトークン（ghp_...）をコピー" -ForegroundColor White
Write-Host ""

$username = Read-Host "GitHubのユーザー名を入力してください"
$token    = Read-Host "Personal Access Token を入力してください（ghp_...）"

git config --global user.name  $username
git config --global user.email "$username@users.noreply.github.com"

# Windows Credential Manager に保存
$credentialEntry = "git:https://github.com"
$secureToken = ConvertTo-SecureString $token -AsPlainText -Force
$credential  = New-Object System.Management.Automation.PSCredential($username, $secureToken)

try {
    cmdkey /add:LegacyGeneric:target=git:https://github.com /user:$username /pass:$token
    Write-Host ""
    Write-Host "認証情報を保存しました。" -ForegroundColor Green
} catch {
    # fallback: .netrc
    $netrc = "$env:USERPROFILE\.netrc"
    Add-Content $netrc "machine github.com login $username password $token"
    Write-Host "認証情報を保存しました（.netrc）。" -ForegroundColor Green
}

Write-Host ""
Write-Host "設定完了！ git push が使えるようになりました。" -ForegroundColor Green
Write-Host ""
Read-Host "Enterキーで閉じる"
