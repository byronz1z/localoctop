#!/usr/bin/env bash
# integration.sh — POSIX counterpart of scripts/integration.ps1 for CI/Linux.
# Starts the mock adapter + bridge, fires tool calls, asserts security denials,
# audit logging, and reconnection. Run from the octop-local-bridge directory:
#
#   bash scripts/integration.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-18443}"
BASE="http://127.0.0.1:${PORT}"
TMP="$(mktemp -d)"
BIN="${TMP}/bin"
mkdir -p "${BIN}"
trap 'kill ${MOCK_PID:-} ${BRIDGE_PID:-} 2>/dev/null || true; rm -rf "${TMP}"' EXIT

printf 'integration-ok' > "${TMP}/hello.txt"
mkdir -p "${TMP}/docs"
printf '# note' > "${TMP}/docs/note.md"

PASS=0; FAIL=0
check() { # name, ok(0/1)
  if [ "$2" = "0" ]; then PASS=$((PASS+1)); echo "  PASS  $1";
  else FAIL=$((FAIL+1)); echo "  FAIL  $1"; fi
}

post_call() {
  curl -sS -m 40 -X POST "${BASE}/call" -H 'Content-Type: application/json' -d "$1"
}

echo "== build =="
go build -o "${BIN}/testserver" ./internal/testserver
go build -o "${BIN}/bridge" ./cmd/bridge
check "go build" 0

echo "== start mock adapter =="
"${BIN}/testserver" -addr "127.0.0.1:${PORT}" -token it-token >"${TMP}/mock.log" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 40); do curl -sS -m 1 "${BASE}/sessions" >/dev/null 2>&1 && break; sleep 0.25; done
curl -sS -m 2 "${BASE}/sessions" >/dev/null; check "mock adapter up" 0

echo "== start bridge client =="
AUDIT="${TMP}/audit.log"
"${BIN}/bridge" --headless -server "ws://127.0.0.1:${PORT}/mcp-localfs/ws" -token it-token -dir "${TMP}" -audit "${AUDIT}" >"${TMP}/bridge.log" 2>&1 &
BRIDGE_PID=$!
for _ in $(seq 1 40); do
  n=$(curl -sS -m 2 "${BASE}/sessions" | grep -o '"client_id"' | wc -l)
  [ "${n:-0}" -ge 1 ] && break; sleep 0.25
done
[ "${n:-0}" -ge 1 ]; check "bridge registered" 0

echo "== tool calls =="
R=$(post_call '{"token":"it-token","method":"list_directory","params":{"path":"."}}')
echo "$R" | grep -q '"hello.txt"'; check "list_directory ok" $?

R=$(post_call '{"token":"it-token","method":"read_file","params":{"path":"hello.txt"}}')
echo "$R" | grep -q 'integration-ok'; check "read_file content" $?

R=$(post_call '{"token":"it-token","method":"get_file_info","params":{"path":"docs/note.md"}}')
echo "$R" | grep -q '"note.md"'; check "get_file_info ok" $?

R=$(post_call '{"token":"it-token","method":"search_files","params":{"path":"docs","pattern":"*.md"}}')
echo "$R" | grep -q 'note.md'; check "search_files glob" $?

R=$(post_call '{"token":"it-token","method":"read_file","params":{"path":"../../etc/passwd"}}')
echo "$R" | grep -q '"code": *4001\|"code":4001'; check "traversal denied (4001)" $?

R=$(post_call '{"token":"it-token","method":"read_file","params":{"path":"/etc/passwd"}}')
echo "$R" | grep -q '"code": *4001\|"code":4001'; check "outside-whitelist denied (4001)" $?

R=$(post_call '{"token":"it-token","method":"write_file","params":{"path":"x.txt","content":"no"}}')
echo "$R" | grep -q '"code": *4004\|"code":4004'; check "write disabled (4004)" $?

R=$(post_call '{"token":"it-token","method":"no_such_tool","params":{}}')
echo "$R" | grep -- '-32601' >/dev/null; check "unknown method (-32601)" $?

echo "== audit log =="
sleep 0.5
lines=$(wc -l < "${AUDIT}" || echo 0)
[ "${lines}" -ge 6 ]; check "audit log written (>=6 lines: ${lines})" $?
grep -q '"decision":"allow"' "${AUDIT}"; check "audit records allow" $?
grep -q '"decision":"deny"' "${AUDIT}"; check "audit records deny" $?

echo "== reconnect =="
kill "${BRIDGE_PID}"; sleep 1
n=$(curl -sS -m 2 "${BASE}/sessions" | grep -o '"client_id"' | wc -l)
[ "${n:-0}" -eq 0 ]; check "session dropped after kill" $?
"${BIN}/bridge" --headless -server "ws://127.0.0.1:${PORT}/mcp-localfs/ws" -token it-token -dir "${TMP}" -audit "${AUDIT}" >>"${TMP}/bridge.log" 2>&1 &
BRIDGE_PID=$!
for _ in $(seq 1 40); do
  n=$(curl -sS -m 2 "${BASE}/sessions" | grep -o '"client_id"' | wc -l)
  [ "${n:-0}" -ge 1 ] && break; sleep 0.25
done
[ "${n:-0}" -ge 1 ]; check "bridge re-registers on restart" $?
R=$(post_call '{"token":"it-token","method":"read_file","params":{"path":"hello.txt"}}')
echo "$R" | grep -q 'integration-ok'; check "call works after reconnect" $?

echo "== bad token =="
"${BIN}/bridge" -server "ws://127.0.0.1:${PORT}/mcp-localfs/ws" -token wrong -dir "${TMP}" >"${TMP}/bridge-bad.log" 2>&1 &
BAD_PID=$!
sleep 2
kill -0 "${BAD_PID}" 2>/dev/null; check "bad-token bridge keeps retrying" $?
grep -q 'handshake failed' "${TMP}/bridge-bad.log"; check "bad-token rejected by adapter" $?
kill "${BAD_PID}" 2>/dev/null || true

echo ""
echo "RESULT: ${PASS} passed, ${FAIL} failed"
[ "${FAIL}" -eq 0 ]
