#!/usr/bin/env bash
# Baked into the runner images; never mounted. Runs with no network, read-only root, UID 10001.
# Lint and security scan cover only the files the patch changed (.factory/changed_files.txt, written
# by the orchestrator), so pre-existing issues in the repo never fail a run.
set -uo pipefail
export PYTHONDONTWRITEBYTECODE=1          # nothing may be written next to the frozen tests
ws="${FACTORY_WORKSPACE:-/workspace}"     # override only for local testing of this script
cd "$ws" || exit 2
out="$ws/.factory"
mkdir -p "$out"

changed() {  # existing changed files matching an extension regex
  [ -f "$out/changed_files.txt" ] || return 0
  grep -E "$1" "$out/changed_files.txt" | while IFS= read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done
}

if [ -f pyproject.toml ] || [ -f setup.py ] || compgen -G "tests/*.py" > /dev/null; then
  mapfile -t files < <(changed '\.py$')
  if [ "${#files[@]}" -gt 0 ]; then
    ruff check --output-format json "${files[@]}" > "$out/lint.json" 2>/dev/null; lint=$?
    bandit -f json -o "$out/qa.json" -q "${files[@]}" > /dev/null 2>&1 || true
  else
    echo '[]' > "$out/lint.json"; lint=0
  fi
  python -m pytest tests -q -p no:cacheprovider --junitxml="$out/junit.xml" --timeout=60 \
    > "$out/test.log" 2>&1; test=$?
else
  # Dependencies were installed into the image from the target repo's lockfile.
  [ -e node_modules ] || ln -s /opt/deps/node_modules node_modules
  mapfile -t files < <(changed '\.(ts|tsx|js|jsx|mjs|cjs)$')
  echo '[]' > "$out/lint.json"; lint=0
  if [ "${#files[@]}" -gt 0 ]; then
    # Style lint uses the repo's own ESLint flat config; ESLint 9 fails outright without one, so skip it then.
    if compgen -G "eslint.config.*" > /dev/null; then
      /opt/factory/node_modules/.bin/eslint -f json "${files[@]}" > "$out/lint.json" 2>/dev/null; lint=$?
    fi
    # Security scan always runs, with the factory's config (see eslint.security.mjs).
    /opt/factory/node_modules/.bin/eslint --no-config-lookup -c /opt/factory/eslint.security.mjs \
      -f json "${files[@]}" > "$out/qa.json" 2>/dev/null || true
  fi
  # The repo's own Jest, baked into the image from its lockfile. No npx: it could fetch or pick another version.
  if [ -x node_modules/.bin/jest ]; then
    node_modules/.bin/jest --ci --json --outputFile="$out/jest.json" --testTimeout=60000 \
      > "$out/test.log" 2>&1; test=$?
  else
    echo "factory: jest not installed in the runner image (add it to devDependencies, then make runner-ts)" \
      > "$out/test.log"; test=127
  fi
fi

printf '{"lint_exit":%d,"test_exit":%d}\n' "$lint" "$test" > "$out/result.json"
exit $(( lint != 0 || test != 0 ))
