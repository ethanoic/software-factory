#!/usr/bin/env bash
# Obtain and store the factory's Claude subscription token, using your current Claude login.
#
#   scripts/claude-token.sh new      run `claude setup-token`, capture the token, store it (default)
#   scripts/claude-token.sh paste    store a token you already have (hidden prompt, or piped on stdin)
#   scripts/claude-token.sh status   show which token is stored and when it expires
#
# `claude setup-token` opens the same browser sign-in as Claude Code's /login. If your browser is
# already signed in to claude.ai, you only click Authorize. It mints a separate one-year token for
# scripts; your interactive Claude Code login is untouched. This script never prints the token.
#
# The token is written to secrets/claude_oauth_token (mode 600) as JSON with its creation time, and
# mounted into the factory containers as a Docker secret. Runs on macOS (bash 3.2) and Linux.
set -euo pipefail

FACTORY_DIR="${FACTORY_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
SECRET="$FACTORY_DIR/secrets/claude_oauth_token"
ENV_FILE="$FACTORY_DIR/.env.factory"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
LIFETIME_DAYS=365
TOKEN_RE='sk-ant-oat[0-9][0-9]-[A-Za-z0-9_-]{20,}'

die() { printf 'claude-token: %s\n' "$*" >&2; exit 1; }
mask() { local t="$1"; printf '%s…%s' "${t:0:13}" "${t: -4}"; }      # sk-ant-oat01-…ab12

valid() { printf '%s' "$1" | grep -Eq "^${TOKEN_RE}\$"; }

store() {
  local token="$1" source="$2" now
  valid "$token" || die "that does not look like a Claude setup-token token (sk-ant-oat…)"
  now="$(date +%s)"
  umask 077
  mkdir -p "$(dirname "$SECRET")"
  # Write in place (same inode), so a running container's bind-mounted secret sees the new value.
  printf '{"token":"%s","created_epoch":%s,"source":"%s","lifetime_days":%s}\n' \
    "$token" "$now" "$source" "$LIFETIME_DAYS" > "$SECRET"
  chmod 600 "$SECRET"
  clear_env_copy
  printf 'Stored %s in %s (expires in about %s days).\n' "$(mask "$token")" "$SECRET" "$LIFETIME_DAYS"
  printf 'Restart the factory to use it: make up   (then check it: make claude-token-check)\n'
}

clear_env_copy() {
  # An older setup kept the token in .env.factory. The secret file now wins, so blank the old copy
  # rather than leave a second, possibly stale, credential lying around.
  [ -f "$ENV_FILE" ] || return 0
  if grep -Eq '^CLAUDE_CODE_OAUTH_TOKEN=.+' "$ENV_FILE"; then
    local tmp
    tmp="$(mktemp "$FACTORY_DIR/.env.factory.XXXXXX")"
    sed 's/^CLAUDE_CODE_OAUTH_TOKEN=.*/CLAUDE_CODE_OAUTH_TOKEN=/' "$ENV_FILE" > "$tmp"
    cat "$tmp" > "$ENV_FILE"          # keep the original file's inode and permissions
    rm -f "$tmp"
    printf 'Cleared the old CLAUDE_CODE_OAUTH_TOKEN value in %s.\n' "$ENV_FILE"
  fi
}

run_capture() {
  # Run `claude setup-token` in a pseudo-terminal you can see and use, while copying its output to a
  # private log. A very wide terminal keeps the token on one line; ANSI codes are stripped afterwards.
  local log="$1" inner
  inner='stty cols 500 2>/dev/null; exec "$0" setup-token'
  if script --version >/dev/null 2>&1; then                   # util-linux (Linux)
    script -q -e -c "bash -c '$inner' '$CLAUDE_BIN'" "$log"
  else                                                         # BSD (macOS)
    script -q "$log" bash -c "$inner" "$CLAUDE_BIN"
  fi
}

extract() {
  tr -d '\r' < "$1" | sed -E $'s/\x1b\\[[0-9;?]*[A-Za-z]//g; s/\x1b\\][^\x07]*\x07//g' \
    | grep -oE "$TOKEN_RE" | tail -n 1 || true
}

cmd_new() {
  command -v "$CLAUDE_BIN" >/dev/null 2>&1 || die "Claude Code is not installed. Install it (https://code.claude.com/docs/en/setup), then run this again."
  local dir log token=""
  dir="$(mktemp -d "${TMPDIR:-/tmp}/claude-token.XXXXXX")"
  chmod 700 "$dir"
  log="$dir/session.log"
  trap 'rm -rf "$dir"' EXIT INT TERM
  printf 'A browser window opens for claude.ai. Approve access there; the token is captured automatically.\n\n'
  if ! run_capture "$log"; then
    printf '\n'
    die "claude setup-token did not finish. Nothing was stored."
  fi
  [ -f "$log" ] && token="$(extract "$log")"
  rm -rf "$dir"; trap - EXIT INT TERM
  printf '\n'
  if [ -z "$token" ]; then
    printf 'Could not read the token from the output. Paste the token claude printed above instead.\n'
    cmd_paste
    return
  fi
  store "$token" "setup-token"
}

cmd_paste() {
  local token
  if [ -t 0 ]; then
    printf 'Token (input hidden): '
    IFS= read -rs token
    printf '\n'
  else
    IFS= read -r token || true
  fi
  token="$(printf '%s' "$token" | tr -d '[:space:]')"
  [ -n "$token" ] || die "no token entered. Nothing was stored."
  store "$token" "pasted"
}

cmd_status() {
  [ -s "$SECRET" ] || { printf 'No token stored in %s. Run: make claude-token\n' "$SECRET"; exit 1; }
  local raw token created source age left
  raw="$(cat "$SECRET")"
  case "$raw" in
    \{*) token="$(printf '%s' "$raw" | sed -nE 's/.*"token" *: *"([^"]*)".*/\1/p')"
         created="$(printf '%s' "$raw" | sed -nE 's/.*"created_epoch" *: *([0-9]+).*/\1/p')"
         source="$(printf '%s' "$raw" | sed -nE 's/.*"source" *: *"([^"]*)".*/\1/p')" ;;
    *)   token="$raw"; created=""; source="raw file" ;;
  esac
  valid "$token" || die "$SECRET does not hold a valid token. Run: make claude-token"
  printf 'Token:   %s (%s)\n' "$(mask "$token")" "$source"
  if [ -n "$created" ]; then
    age=$(( ($(date +%s) - created) / 86400 ))
    left=$(( LIFETIME_DAYS - age ))
    printf 'Age:     %s days; expires in about %s days\n' "$age" "$left"
    [ "$left" -gt 30 ] || printf 'Renew soon: make claude-token\n'
  else
    printf 'Age:     unknown (stored without a creation date). Renew with make claude-token to track expiry.\n'
  fi
}

case "${1:-new}" in
  new)    cmd_new ;;
  paste)  cmd_paste ;;
  status) cmd_status ;;
  -h|--help|help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$1' (use new, paste or status)" ;;
esac
