#!/bin/bash
# Fantasy manager routine — fired by launchd (see README) Mon-Fri at 08:04 and 21:04,
# Sundays at 09:04, 11:04 and 21:04. No Saturday runs.
set -euo pipefail

# launchd starts with a minimal PATH; claude lives in ~/.local/bin, python3 in homebrew.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Run from the repo root, wherever this script lives.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

set -a
source .env
set +a

LOG="$REPO_DIR/launchd.log"

# Record which claude binary launchd actually resolves, and its version, before anything can exit.
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') preflight ==="
  echo "which claude: $(command -v claude || echo 'NOT ON PATH')"
  echo "claude --version: $(claude --version 2>&1 || echo 'failed')"
  echo "auth source: $([ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && echo 'CLAUDE_CODE_OAUTH_TOKEN (from .env)' || echo 'keychain (no token in .env)')"
} >> "$LOG"

if [ ! -s run-prompt.md ]; then
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') run-prompt.md missing or empty, skipping ===" >> "$LOG"
  exit 1
fi

auth_expired() {
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') AUTH EXPIRED — run /login on this machine ===" >> "$LOG"
}

# Skip the run if claude has no credentials. `auth status` only reads local state, so an
# OAuth session that expired server-side can still pass; the post-run check below catches that.
if ! claude auth status 2>/dev/null \
    | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("loggedIn") else 1)' 2>/dev/null; then
  auth_expired
  exit 1
fi

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') run start ===" >> "$LOG"
log_start=$(wc -c < "$LOG")
OUT=$(mktemp -t fantasy-run)
rc=0
# Sonnet only: --model for the session, CLAUDE_CODE_SUBAGENT_MODEL so no subagent can pick Opus.
# JSON output so the run's token usage can be logged; the brief text is unpacked below.
CLAUDE_CODE_SUBAGENT_MODEL=sonnet claude -p "$(cat run-prompt.md)" \
  --model sonnet --effort medium --output-format json \
  --dangerously-skip-permissions > "$OUT" 2>> "$LOG" || rc=$?
# Brief text into the log as before, then one grep-able "tokens:" line (all models, subagents included).
python3 - "$OUT" >> "$LOG" 2>&1 <<'PY' || true
import json, sys
raw = open(sys.argv[1]).read()
try:
    r = json.loads(raw)
except ValueError:
    print(raw)
    print("tokens: unavailable (claude output was not JSON)")
    sys.exit(0)
print(r.get("result", ""))
total, parts = 0, []
for model, u in (r.get("modelUsage") or {}).items():
    inp, out = u.get("inputTokens", 0), u.get("outputTokens", 0)
    cr, cw = u.get("cacheReadInputTokens", 0), u.get("cacheCreationInputTokens", 0)
    total += inp + out + cr + cw
    parts.append(f"{model} in={inp} out={out} cache_read={cr} cache_write={cw} "
                 f"searches={u.get('webSearchRequests', 0)}")
print(f"tokens: total={total} est_cost=${r.get('total_cost_usd') or 0:.2f} turns={r.get('num_turns')} | "
      + " | ".join(parts))
PY
rm -f "$OUT"
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') run end (exit $rc) ===" >> "$LOG"
# No grep -q: it exits on first match, SIGPIPEs tail, and pipefail would turn a match into false.
if tail -c +$((log_start + 1)) "$LOG" | grep -E 'Failed to authenticate|Invalid auth token' > /dev/null; then
  auth_expired
fi
exit $rc
