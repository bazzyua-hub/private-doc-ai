# LocalDoc AI: モデル（Qwen3-1.7B・Q8_0・約1.8GB）をダウンロードして models フォルダに置きます。
# ネット接続を使うのは、この操作だけです（PDFの内容は一切送信されません）。
# 実行: get_model.bat をダブルクリック
param(
    [string]$Repo = "Qwen/Qwen3-1.7B-GGUF",
    [string]$Quant = "Q8_0"
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"      # 進捗バーを出さない方が、Windows PowerShell 5.1 では何倍も速い

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$models = Join-Path $root "models"
New-Item -ItemType Directory -Force -Path $models | Out-Null

Write-Host "モデルの情報を取得しています: $Repo"
$info = Invoke-RestMethod -Uri "https://huggingface.co/api/models/${Repo}?blobs=true" -Headers @{ "User-Agent" = "LocalDocAI-setup" }
$file = $info.siblings | Where-Object {
    $_.rfilename -like "*$Quant.gguf" -and $_.rfilename -notmatch "mmproj" -and $_.rfilename -notmatch "-\d{5}-of-\d{5}"
} | Select-Object -First 1
if (-not $file) {
    throw "量子化 '$Quant' のファイルが見つかりませんでした。"
}

$dest = Join-Path $models $file.rfilename
if (Test-Path $dest) {
    Write-Host "すでにあります: $dest"
    exit 0
}
$part = "$dest.part"
$url = "https://huggingface.co/$Repo/resolve/main/$($file.rfilename)"
Write-Host "ダウンロードしています（約1.8GB・数分かかります。画面が止まって見えても待ってください）:"
Write-Host "  $url"
Invoke-WebRequest -Uri $url -OutFile $part -UseBasicParsing -Headers @{ "User-Agent" = "LocalDocAI-setup" }
$size = (Get-Item $part).Length
if ($size -lt 100MB) {
    Remove-Item $part -Force
    throw "ダウンロードしたファイルが小さすぎます（$size バイト）。もう一度試してください。"
}
$expected = $file.lfs.sha256
if ($expected) {
    Write-Host "ファイルの整合性（SHA-256）を確認しています…"
    $actual = (Get-FileHash -Path $part -Algorithm SHA256).Hash
    if ($actual -ne $expected.ToUpper()) {
        Remove-Item $part -Force
        throw "ダウンロードしたファイルが壊れています（ハッシュが一致しません）。もう一度試してください。"
    }
}
Move-Item $part $dest -Force
Write-Host "完了しました: $dest"
