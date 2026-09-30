# sync-certs.ps1 — 从 octop 容器同步 LE 证书到 localoctop-server 部署目录
#
# 背景：证书由 Octop 内置 ACME 客户端签发续期，落在 octop_data 卷
#       （容器内 /data/.octop/ssl/，root:600）。Docker Desktop 的命名卷
#       无法以非 root 只读方式 bind 给适配器容器，故用 docker cp 拷出。
# 时机：Octop 证书续期后（约每 60 天，可在 Octop 管理页看到到期日）跑一次，
#       然后 docker compose restart 适配器容器。
# 用法：在 ops 仓任意位置执行，或加入计划任务（每年 1、5、9 月各一次即可）。

$ErrorActionPreference = 'Stop'
$dst = if ($args[0]) { $args[0] } else { 'D:\SelfHosted\localoctop-server' }

docker cp octop:/data/.octop/ssl/fullchain.pem "$dst\fullchain.pem"
docker cp octop:/data/.octop/ssl/privkey.pem  "$dst\privkey.pem"

Write-Host "certs synced to $dst"
Write-Host "next: cd $dst ; docker compose restart localoctop-server"
Write-Host "verify: curl -sk https://127.0.0.1:8445/healthz"
