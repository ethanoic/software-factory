#!/bin/zsh
# Writes macOS memory headroom and local model sizes for the orchestrator (mounted read-only).
# Installed as a launchd agent by `make hoststat`. Directory names in OMLX_MODELS_DIR must equal
# the model aliases your omlx server exposes.
set -u
FACTORY_DIR="${FACTORY_DIR:-$HOME/software-factory}"
OMLX_MODELS_DIR="${OMLX_MODELS_DIR:-$HOME/models/mlx}"
out="$FACTORY_DIR/hoststat/hoststat.json"

pct=$(memory_pressure -Q 2>/dev/null | awk -F': ' '/free percentage/ {gsub("%","",$2); print $2}')
models=""
if [ -d "$OMLX_MODELS_DIR" ]; then
  for d in "$OMLX_MODELS_DIR"/*(N/); do
    n="${d:t}"
    gb=$(du -sk "$d" | awk '{printf "%.2f", $1/1048576}')
    models+="\"$n\":$gb,"
  done
fi
printf '{"ts":%d,"free_pct":%s,"local_models":{%s}}\n' "$(date +%s)" "${pct:-0}" "${models%,}" > "$out.tmp"
mv "$out.tmp" "$out"
