# allow-cfw.ps1 — 放行 localoctop 公网桥接端口（需管理员运行一次）
# 员工桥经路由器端口转发拨入本机 8445，Windows 防火墙默认拦截入站，需放行。
# 用法：右键"以管理员身份运行 PowerShell" → 执行本脚本 → 回显规则确认。

$ErrorActionPreference = 'Stop'
$adm = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $adm) { Write-Error "请以管理员身份运行本脚本"; exit 1 }

New-NetFirewallRule -DisplayName 'localoctop bridge 8445' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8445 -Profile Any | Out-Null

Get-NetFirewallRule -DisplayName 'localoctop bridge 8445' |
    Select-Object DisplayName, Enabled, Direction, Action |
    Format-Table -AutoSize

Write-Host 'done. (verify: curl -sk https://127.0.0.1:8445/healthz)'
