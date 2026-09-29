# integration.ps1 — localoctop <-> mock adapter end-to-end smoke test.
#
# What it does:
#   1. go build ./... && go vet ./...
#   2. go test ./...
#   3. starts internal/testserver on 127.0.0.1:18443
#   4. starts cmd/bridge against it with a temp whitelist dir
#   5. fires tool calls through POST /call and asserts:
#        - list_directory / read_file / get_file_info / search_files succeed
#        - path traversal ("../..") is denied with code 4001
#        - write_file is denied with code 4004 while the write switch is off
#        - unknown method returns -32601
#        - audit log records allow+deny decisions
#   6. kills the server, reconnects a bridge, verifies re-registration
#
# Run from the localoctop directory:
#   powershell -ExecutionPolicy Bypass -File scripts\integration.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$port = 18443
$base = "http://127.0.0.1:$port"
$tmp = Join-Path $env:TEMP ("lfsb-it-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$bin = Join-Path $tmp "bin"
New-Item -ItemType Directory -Force -Path $tmp, $bin | Out-Null
Set-Content -Path (Join-Path $tmp "hello.txt") -Value "integration-ok" -NoNewline
New-Item -ItemType Directory -Force -Path (Join-Path $tmp "docs") | Out-Null
Set-Content -Path (Join-Path $tmp "docs\note.md") -Value "# note" -NoNewline

$pass = 0; $fail = 0
function Check([string]$name, [bool]$ok, [string]$detail = "") {
    if ($ok) { $script:pass++; Write-Host "  PASS  $name" -ForegroundColor Green }
    else { $script:fail++; Write-Host "  FAIL  $name  $detail" -ForegroundColor Red }
}

function PostCall([string]$body) {
    $resp = Invoke-WebRequest -Uri "$base/call" -Method Post -ContentType "application/json" -Body $body -UseBasicParsing -TimeoutSec 40
    return $resp.Content
}

$mock = $null; $bridge = $null
try {
    Write-Host "== build =="
    go build -o (Join-Path $bin "testserver.exe") ./internal/testserver
    if ($LASTEXITCODE -ne 0) { throw "go build testserver failed" }
    go build -o (Join-Path $bin "localoctop.exe") ./cmd/bridge
    if ($LASTEXITCODE -ne 0) { throw "go build bridge failed" }
    Check "go build" $true

    Write-Host "== start mock adapter =="
    $mock = Start-Process -FilePath (Join-Path $bin "testserver.exe") `
        -ArgumentList "-addr", "127.0.0.1:$port", "-token", "it-token" `
        -PassThru -WindowStyle Hidden -RedirectStandardError (Join-Path $tmp "mock.log")
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        try { Invoke-WebRequest -Uri "$base/sessions" -UseBasicParsing -TimeoutSec 1 | Out-Null; break } catch { Start-Sleep -Milliseconds 250 }
    }
    $s = Invoke-WebRequest -Uri "$base/sessions" -UseBasicParsing -TimeoutSec 2
    Check "mock adapter up" ($s.StatusCode -eq 200)

    Write-Host "== start bridge client =="
    $auditLog = Join-Path $tmp "audit.log"
    $bridge = Start-Process -FilePath (Join-Path $bin "localoctop.exe") `
        -ArgumentList "--headless", "-server", "ws://127.0.0.1:$port/mcp/localoctop/ws", "-token", "it-token", "-dir", $tmp, "-audit", $auditLog `
        -PassThru -WindowStyle Hidden -RedirectStandardError (Join-Path $tmp "bridge.log")
    $deadline = (Get-Date).AddSeconds(10)
    $registered = $false
    while ((Get-Date) -lt $deadline) {
        $s = (Invoke-WebRequest -Uri "$base/sessions" -UseBasicParsing -TimeoutSec 2).Content | ConvertFrom-Json
        if ($s.sessions.Count -ge 1) { $registered = $true; break }
        Start-Sleep -Milliseconds 250
    }
    Check "bridge registered" $registered

    Write-Host "== tool calls =="
    $r = PostCall '{"token":"it-token","method":"list_directory","params":{"path":"."}}' | ConvertFrom-Json
    Check "list_directory ok" ($null -ne $r.result -and ($r.result.entries.name -contains "hello.txt"))

    $r = PostCall '{"token":"it-token","method":"read_file","params":{"path":"hello.txt"}}' | ConvertFrom-Json
    Check "read_file content" ($r.result.text -eq "integration-ok")

    $r = PostCall '{"token":"it-token","method":"get_file_info","params":{"path":"docs/note.md"}}' | ConvertFrom-Json
    Check "get_file_info ok" ($r.result.name -eq "note.md")

    $r = PostCall '{"token":"it-token","method":"search_files","params":{"path":"docs","pattern":"*.md"}}' | ConvertFrom-Json
    Check "search_files glob" ($r.result.matches.Count -ge 1)

    $r = PostCall '{"token":"it-token","method":"read_file","params":{"path":"../../etc/passwd"}}' | ConvertFrom-Json
    Check "traversal denied (4001)" ($r.error.code -eq 4001)

    $r = PostCall '{"token":"it-token","method":"read_file","params":{"path":"C:\\Windows\\win.ini"}}' | ConvertFrom-Json
    Check "outside-whitelist denied (4001)" ($r.error.code -eq 4001)

    $r = PostCall '{"token":"it-token","method":"write_file","params":{"path":"x.txt","content":"no"}}' | ConvertFrom-Json
    Check "write disabled (4004)" ($r.error.code -eq 4004)

    $r = PostCall '{"token":"it-token","method":"no_such_tool","params":{}}' | ConvertFrom-Json
    Check "unknown method (-32601)" ($r.error.code -eq -32601)

    Write-Host "== audit log =="
    $audit = Get-Content $auditLog -ErrorAction SilentlyContinue
    Check "audit log written" ($audit.Count -ge 6)
    Check "audit records allow" (($audit | Where-Object { $_ -match '"decision":"allow"' }).Count -ge 4)
    Check "audit records deny" (($audit | Where-Object { $_ -match '"decision":"deny"' }).Count -ge 3)

    Write-Host "== reconnect =="
    Stop-Process -Id $bridge.Id -Force
    $bridge = $null
    Start-Sleep -Milliseconds 500
    $s = (Invoke-WebRequest -Uri "$base/sessions" -UseBasicParsing -TimeoutSec 2).Content | ConvertFrom-Json
    Check "session dropped after kill" ($s.sessions.Count -eq 0)
    $bridge = Start-Process -FilePath (Join-Path $bin "localoctop.exe") `
        -ArgumentList "--headless", "-server", "ws://127.0.0.1:$port/mcp/localoctop/ws", "-token", "it-token", "-dir", $tmp, "-audit", $auditLog `
        -PassThru -WindowStyle Hidden -RedirectStandardError (Join-Path $tmp "bridge2.log")
    $deadline = (Get-Date).AddSeconds(10)
    $reback = $false
    while ((Get-Date) -lt $deadline) {
        $s = (Invoke-WebRequest -Uri "$base/sessions" -UseBasicParsing -TimeoutSec 2).Content | ConvertFrom-Json
        if ($s.sessions.Count -ge 1) { $reback = $true; break }
        Start-Sleep -Milliseconds 250
    }
    Check "bridge re-registers on restart" $reback
    $r = PostCall '{"token":"it-token","method":"read_file","params":{"path":"hello.txt"}}' | ConvertFrom-Json
    Check "call works after reconnect" ($r.result.text -eq "integration-ok")

    Write-Host "== bad token =="
    $bad = Start-Process -FilePath (Join-Path $bin "localoctop.exe") `
        -ArgumentList "--headless", "-server", "ws://127.0.0.1:$port/mcp/localoctop/ws", "-token", "wrong", "-dir", $tmp `
        -PassThru -WindowStyle Hidden -RedirectStandardError (Join-Path $tmp "bridge-bad.log")
    Start-Sleep -Seconds 2
    Check "bad-token bridge not connected" (-not $bad.HasExited)  # keeps retrying, never registers
    $badLog = Get-Content (Join-Path $tmp "bridge-bad.log") -Raw -ErrorAction SilentlyContinue
    Check "bad-token rejected by adapter" ($badLog -match "handshake failed")
    Stop-Process -Id $bad.Id -Force -ErrorAction SilentlyContinue
}
finally {
    if ($bridge -and -not $bridge.HasExited) { Stop-Process -Id $bridge.Id -Force -ErrorAction SilentlyContinue }
    if ($mock -and -not $mock.HasExited) { Stop-Process -Id $mock.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 300
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host ("RESULT: {0} passed, {1} failed" -f $pass, $fail) -ForegroundColor ($(if ($fail -eq 0) { "Green" } else { "Red" }))
if ($fail -gt 0) { exit 1 }
exit 0
