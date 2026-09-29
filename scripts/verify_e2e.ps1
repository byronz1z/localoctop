# verify_e2e.ps1 — end-to-end acceptance for T-06 (adapter <-> bridge <-> MCP probe).
#
# Three stages, all real processes on this machine:
#   1. start the mcp-localfs adapter (uvicorn, from adapter/.venv)
#   2. start the real bridge EXE (dist/bridge.exe --headless) dialing into it
#   3. probe the MCP endpoint exactly like Octop's custom-MCP connector
#      (initialize / tools/list / tools/call), then verify audit trails on
#      BOTH sides (bridge JSONL + adapter JSONL).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\verify_e2e.ps1
# Optional overrides:
#   -Port 18080            adapter listen port
#   -BridgeExe dist\bridge.exe
#
# Exits 0 only when every stage and every audit check passes.

param(
    [int]$Port = 18080,
    [string]$BridgeExe = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$AdapterDir = Join-Path $RepoRoot "adapter"
$Py = Join-Path $AdapterDir ".venv\Scripts\python.exe"

# --- locate the bridge EXE (dist artifact first; build it when missing) ----
if (-not $BridgeExe) {
    $BridgeExe = Join-Path $RepoRoot "dist\bridge.exe"
    if (-not (Test-Path $BridgeExe)) {
        Write-Host "[e2e] dist\bridge.exe not found — building from source (go build)"
        Push-Location $RepoRoot
        try { go build -o dist\bridge.exe .\cmd\bridge } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { throw "go build failed" }
    }
}
if (-not (Test-Path $BridgeExe)) { throw "bridge EXE not found: $BridgeExe" }
if (-not (Test-Path $Py)) { throw "adapter venv missing — run: python -m venv .venv && pip install -r requirements.txt (in adapter/)" }

# --- scratch space -----------------------------------------------------------
$Work = Join-Path $env:TEMP ("e2e-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$DataDir = Join-Path $Work "data"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
Set-Content -Path (Join-Path $DataDir "hello-e2e.txt") -Value "e2e-ok" -Encoding utf8 -NoNewline
$AdapterAudit = Join-Path $Work "adapter-audit.jsonl"
$BridgeAudit = Join-Path $Work "bridge-audit.jsonl"
$AdapterLog = Join-Path $Work "adapter.log"
$BridgeLog = Join-Path $Work "bridge.log"

# --- fixed e2e credentials (split token model: mcp vs bridge) ----------------
$McpToken = "e2e-mcp-token"
$BridgeToken = "e2e-bridge-token"
$BaseUrl = "http://127.0.0.1:$Port"

$adapterProc = $null
$bridgeProc = $null
$failed = $false

function Step([string]$name) { Write-Host "" ; Write-Host "== $name" }

try {
    Step "stage 1: start mcp-localfs adapter on port $Port"
    # Children of this script inherit the environment — the adapter reads all
    # of its configuration from LOCALFS_* variables.
    $env:LOCALFS_HOST = "127.0.0.1"
    $env:LOCALFS_PORT = "$Port"
    $env:LOCALFS_TOKENS = "e2e-user:$McpToken"
    $env:LOCALFS_BRIDGE_TOKENS = "e2e-user:$BridgeToken"
    $env:LOCALFS_AUDIT_LOG = $AdapterAudit
    $env:LOCALFS_LOG_LEVEL = "INFO"

    $adapterProc = Start-Process -FilePath $Py -ArgumentList "-m", "mcp_localfs.main" `
        -WorkingDirectory $AdapterDir `
        -RedirectStandardOutput $AdapterLog -RedirectStandardError (Join-Path $Work "adapter.err.log") `
        -PassThru -WindowStyle Hidden

    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $h = Invoke-RestMethod -Uri "$BaseUrl/healthz" -TimeoutSec 2
            if ($h.status -eq "ok") { $ok = $true; break }
        } catch { }
    }
    if (-not $ok) { throw "adapter did not become healthy within 30s (see $AdapterLog)" }
    Write-Host "  adapter healthy: $BaseUrl/healthz"

    Step "stage 2: start bridge EXE (headless) dialing ws://127.0.0.1:$Port/mcp-localfs/ws"
    $bridgeProc = Start-Process -FilePath $BridgeExe `
        -ArgumentList "-headless", "-server", "ws://127.0.0.1:$Port/mcp-localfs/ws", `
                      "-token", $BridgeToken, "-dir", $DataDir, "-audit", $BridgeAudit `
        -RedirectStandardOutput (Join-Path $Work "bridge.out.log") -RedirectStandardError $BridgeLog `
        -PassThru -WindowStyle Hidden

    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $h = Invoke-RestMethod -Uri "$BaseUrl/healthz" -TimeoutSec 2
            if ($h.bridges_connected -ge 1) { $ok = $true; break }
        } catch { }
    }
    if (-not $ok) { throw "bridge did not register within 30s (see $BridgeLog)" }
    Write-Host "  bridge registered (healthz bridges_connected >= 1)"

    Step "stage 3: MCP probe (initialize / tools/list / tools/call) as Octop connector"
    & $Py (Join-Path $AdapterDir "scripts\e2e_probe.py") `
        --base-url $BaseUrl --token $McpToken `
        --expect-file "hello-e2e.txt" --expect-content "e2e-ok"
    if ($LASTEXITCODE -ne 0) { throw "MCP probe failed" }

    Step "audit verification (both sides)"
    # Bridge side: compact JSON lines from audit.go
    $bridgeAuditText = ""
    for ($i = 0; $i -lt 20; $i++) {
        if (Test-Path $BridgeAudit) { $bridgeAuditText = Get-Content $BridgeAudit -Raw -ErrorAction SilentlyContinue }
        if ($bridgeAuditText -match '"decision":"allow"' -and $bridgeAuditText -match '"decision":"deny"') { break }
        Start-Sleep -Milliseconds 250
    }
    if ($bridgeAuditText -notmatch '"decision":"allow"') { throw "bridge audit: no allow record ($BridgeAudit)" }
    if ($bridgeAuditText -notmatch '"decision":"deny"') { throw "bridge audit: no deny record ($BridgeAudit)" }
    if ($bridgeAuditText -notmatch '"method":"list_directory"') { throw "bridge audit: list_directory missing" }
    Write-Host "  PASS  bridge JSONL audit has allow + deny + list_directory records"

    # Adapter side: python json.dumps spacing ('"decision": "allow"')
    $adapterAuditText = ""
    for ($i = 0; $i -lt 20; $i++) {
        if (Test-Path $AdapterAudit) { $adapterAuditText = Get-Content $AdapterAudit -Raw -ErrorAction SilentlyContinue }
        if ($adapterAuditText -match '"decision": "allow"' -and $adapterAuditText -match '"decision": "deny"') { break }
        Start-Sleep -Milliseconds 250
    }
    if ($adapterAuditText -notmatch '"decision": "allow"') { throw "adapter audit: no allow record ($AdapterAudit)" }
    if ($adapterAuditText -notmatch '"decision": "deny"') { throw "adapter audit: no deny record ($AdapterAudit)" }
    if ($adapterAuditText -notmatch '"user": "e2e-user"') { throw "adapter audit: user binding missing" }
    Write-Host "  PASS  adapter JSONL audit has allow + deny records bound to e2e-user"

    Write-Host ""
    Write-Host "E2E RESULT: ALL STAGES PASSED"
} catch {
    $failed = $true
    Write-Host ""
    Write-Host "E2E FAILED: $_" -ForegroundColor Red
    if (Test-Path $AdapterLog) { Write-Host "--- adapter log (tail) ---"; Get-Content $AdapterLog -Tail 15 }
    if (Test-Path $BridgeLog) { Write-Host "--- bridge log (tail) ---"; Get-Content $BridgeLog -Tail 15 }
} finally {
    foreach ($p in @($bridgeProc, $adapterProc)) {
        if ($p -and -not $p.HasExited) {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 300
    # Belt and braces: free the port if anything is still bound to it.
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Write-Host ""
    Write-Host "work dir kept for inspection: $Work"
}

if ($failed) { exit 1 } else { exit 0 }
