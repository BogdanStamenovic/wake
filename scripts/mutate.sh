#!/usr/bin/env bash
# Mutation audit: flip one thing, run the suite, report whether it noticed.
#
# A test that compares a value against the constant that produced it passes
# whatever that constant becomes. Seven of wake's tests were that shape until
# this script said so -- including the bind address, both loop periods, and
# the merge tie-break rule. MISSED means the suite has no opinion about that
# line; either pin it or decide out loud that it does not matter.
#
# Restores from git after every mutation, so the tree must be clean first.
# Usage: scripts/mutate.sh
set -uo pipefail
# No `set -e` here, so an unchecked cd would leave the mutation and the
# `git checkout` that undoes it pointed at whatever directory this was
# invoked from.
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
[[ -z "$(git status --porcelain)" ]] || { echo "tree dirty; refusing" >&2; exit 1; }

run_one() {
  local file="$1" from="$2" to="$3" label="$4"
  # perl -0 rather than sed: some mutations span two lines (sync's ordering).
  perl -0pi -e "s|\Q${from}\E|${to}|" "$file"
  if ! grep -qF "$to" "$file"; then
    echo "  SKIP  ${label} (pattern did not apply)"; git checkout -- "$file"; return
  fi
  # Same size + same mtime second reuses a stale .pyc and lies about the result.
  find . -name __pycache__ -type d -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null
  if .venv/bin/pytest -q -x -p no:cacheprovider >/dev/null 2>&1; then
    echo "  MISSED  ${label}"
  else
    echo "  caught  ${label}"
  fi
  git checkout -- "$file"
}

echo "--- constants ---"
run_one src/wake/backends.py "WOL_PORT = 9"            "WOL_PORT = 8"              "WOL_PORT 9->8"
run_one src/wake/backends.py "SHELL_TIMEOUT = 300.0"   "SHELL_TIMEOUT = 0.001"     "SHELL_TIMEOUT 300->0.001"
run_one src/wake/backends.py 'DEFAULT_BROADCAST = "255.255.255.255"' 'DEFAULT_BROADCAST = "127.0.0.1"' "DEFAULT_BROADCAST -> loopback"
run_one src/wake/config.py   "DEFAULT_PORT = 8788"     "DEFAULT_PORT = 9999"       "DEFAULT_PORT 8788->9999"
run_one src/wake/config.py   "poll_seconds: float = 5.0" "poll_seconds: float = 0.0" "poll_seconds 5->0"
run_one src/wake/config.py   "sync_seconds: float = 60.0" "sync_seconds: float = 0.0" "sync_seconds 60->0"
run_one src/wake/config.py   'bind: str = "0.0.0.0"'     'bind: str = "127.0.0.1"'   "bind -> loopback"
run_one src/wake/server.py   "MAX_BODY = 1 << 20"       "MAX_BODY = 1"              "MAX_BODY -> 1 byte"

echo "--- logic ---"
run_one src/wake/db.py "existing.updated_at >= incoming.updated_at" "existing.updated_at > incoming.updated_at" "merge tie: >= -> > (ties now overwrite)"
run_one src/wake/db.py "AND at <= ? " "AND at < ? " "due: <= -> < (exact-time task never fires)"
run_one src/wake/db.py "pushed_rev < rev ORDER BY rev" "pushed_rev <= rev ORDER BY rev" "unpushed: < -> <= (pushes everything forever)"
run_one src/wake/db.py "if expect_rev is not None and existing.rev != expect_rev:" "if False:" "CAS disabled (the self-re-arm clobber)"
run_one src/wake/db.py "AND backend != 'rtcwake'" "AND backend != 'nope'" "due no longer excludes rtcwake"
run_one src/wake/whenspec.py 'if text.startswith(("+", "-")):' "if False:" "unit-less +5 accepted again"
run_one src/wake/server.py 'if task_id and "updated_at" not in body:' "if False:" "HTTP re-arm falls back to merge"
run_one src/wake/syncclient.py "    pushed = push(db, config)
    pulled = pull(db, config)" "    pulled = pull(db, config)
    pushed = push(db, config)" "sync pulls before it pushes"
run_one src/wake/syncclient.py "        db.mark_pushed(task.id, task.rev)" "        pass" "push never acknowledges a row"
