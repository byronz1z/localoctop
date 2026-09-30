# 生成 staging 自签证书（生产 LE 证书绝不复用）
# 用法: powershell -File scripts\mkcert-staging.ps1
$ErrorActionPreference = "Stop"
$out = Join-Path $PSScriptRoot "..\certs-staging"
New-Item -ItemType Directory -Force -Path $out | Out-Null

$dns = "localhost"
$cn  = "localoctop-staging"

$openssl = (Get-Command openssl -ErrorAction SilentlyContinue).Source
if (-not $openssl) { Write-Error "需要 openssl（Git for Windows 自带）"; exit 1 }

& $openssl req -x509 -newkey rsa:2048 -nodes -days 365 `
  -keyout "$out\privkey.pem" -out "$out\fullchain.pem" `
  -subj "/CN=$cn" -addext "subjectAltName=DNS:$dns,DNS:127.0.0.1" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "openssl 失败"; exit 1 }

Write-Output "staging 自签证书已生成: $out\{fullchain,privkey}.pem (CN=$cn, SAN=$dns,127.0.0.1, 365天)"
