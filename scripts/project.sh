#!/usr/bin/env bash
# Project commands that need both the factory and Docker on this computer (called by make).
#   scripts/project.sh add <owner/name> <language> [branch] [max-runs-per-day]
#   scripts/project.sh rebuild <owner/name>
#   scripts/project.sh rebuild-all
# The factory clones the repo (hardened git, inside its own container) and hands back only the
# dependency files; this script builds that project's runner image from them and registers it.
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE=${COMPOSE:-docker compose --env-file .env.factory -f compose.factory.yml}
factory() { $COMPOSE exec -T orchestrator python -m factory.projects "$@"; }

need_factory() {
  if [ -z "$($COMPOSE ps -q orchestrator 2>/dev/null)" ]; then
    echo "The factory is not running. Start it first: make up" >&2; exit 1
  fi
}

rebuild() {
  local repo=$1 dir
  dir="build/projects/$(printf '%s' "$repo" | tr -c 'A-Za-z0-9' '-')"
  rm -rf "$dir" && mkdir -p "$dir/deps"
  echo "==> $repo: fetching its dependency files"
  factory deps "$repo" > "$dir/deps.tar"
  tar -xf "$dir/deps.tar" -C "$dir/deps"
  local hash lang image
  hash=$(cat "$dir/deps/.deps-hash"); lang=$(cat "$dir/deps/.language"); image=$(cat "$dir/deps/.image")
  rm -f "$dir/deps/.deps-hash" "$dir/deps/.language" "$dir/deps/.image" "$dir/deps.tar"
  cp runner-images/entrypoint.sh "$dir/"
  case "$lang" in
    python) cp runner-images/python/Dockerfile "$dir/" ;;
    node|javascript|typescript)
      mkdir -p "$dir/typescript"
      cp runner-images/typescript/Dockerfile "$dir/"
      cp runner-images/typescript/eslint.security.mjs "$dir/typescript/" ;;
    rust)
      mkdir -p "$dir/rust"
      cp runner-images/rust/Dockerfile "$dir/"
      cp runner-images/rust/nextest.toml "$dir/rust/" ;;
    java)  cp runner-images/java/Dockerfile "$dir/" ;;
    godot) cp runner-images/godot/Dockerfile "$dir/" ;;
    *) echo "error: no runner for language '$lang'" >&2; exit 1 ;;
  esac
  echo "==> $repo: building $image ($(ls "$dir/deps" | tr '\n' ' '))"
  docker build -q --label "factory.project=$repo" -t "$image" "$dir" > /dev/null
  docker run --rm --network none --entrypoint test "$image" -x /opt/factory/entrypoint.sh \
    || { echo "error: $image has no runner entrypoint" >&2; exit 1; }
  factory set-image "$repo" "$image" "$hash"
}

cmd=${1:-}; shift || true
case "$cmd" in
  add)
    [ $# -ge 2 ] || { echo "usage: make project-add REPO=owner/name LANGUAGE=python|node|rust|java|godot [BRANCH=main] [MAX_RUNS=n]" >&2; exit 1; }
    need_factory
    args=("$1" --language "$2" --branch "${3:-main}")
    [ -n "${4:-}" ] && args+=(--max-runs-per-day "$4")
    factory add "${args[@]}"
    rebuild "$1"
    echo "==> $1 is ready. Label an issue factory:ready, or drop a ticket in workspace/inbox/."
    ;;
  rebuild)
    [ $# -ge 1 ] || { echo "usage: make project-rebuild REPO=owner/name" >&2; exit 1; }
    need_factory; rebuild "$1"
    ;;
  rebuild-all)
    need_factory
    for r in $(factory names); do rebuild "$r"; done
    ;;
  *) echo "usage: scripts/project.sh add|rebuild|rebuild-all ..." >&2; exit 1 ;;
esac
