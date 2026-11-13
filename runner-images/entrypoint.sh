#!/usr/bin/env bash
# Baked into the runner images; never mounted. Runs with no network, read-only root, UID 10001.
# The broker copies the source and frozen tests in (no .git, no shared folders) and copies .factory/ out.
# Lint and security scan cover only the files the patch changed (.factory/changed_files.txt, written
# by the broker), so pre-existing issues in the repo never fail a run.
#
# Each image sets FACTORY_RUNNER (python | node | rust | java | godot). Images built before that existed
# do not, so the runner is then detected from the files: Python if it looks like Python, else Node.
# Every runner writes the same files: test.log, result.json, and junit.xml or jest.json; lint.json and
# qa.json when it has a linter or security scanner for the changed files.
set -uo pipefail
export PYTHONDONTWRITEBYTECODE=1          # nothing may be written next to the frozen tests
ws="${FACTORY_WORKSPACE:-/workspace}"     # these five: override only for local testing of this script
fh="${FACTORY_HOME:-/opt/factory}"        # factory's own config files in the image
m2="${FACTORY_M2:-/opt/m2}"               # Maven dependencies resolved at image build
godot="${FACTORY_GODOT:-/opt/godot/godot}"
deps="${FACTORY_DEPS:-/opt/deps}"         # dependencies baked into the image
cd "$ws" || exit 2
out="$ws/.factory"
mkdir -p "$out"
lint=0; test=0

changed() {  # existing changed files matching an extension regex
  [ -f "$out/changed_files.txt" ] || return 0
  grep -E "$1" "$out/changed_files.txt" | while IFS= read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done
}

json_str() {  # a JSON string literal; control characters dropped
  local s=${1//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$(printf '%s' "$s" | tr -d '\000-\037')"
}

lint_json() {  # $1: file with one problem per line -> ESLint-shaped lint.json, which the factory counts
  local n first=1 line
  n=$(grep -c . "$1" 2>/dev/null || true)
  {
    printf '[{"filePath":"changed files","errorCount":%d,"messages":[' "${n:-0}"
    while IFS= read -r line; do
      [ "$first" = 1 ] || printf ','
      first=0
      printf '{"message":%s}' "$(json_str "${line:0:300}")"
    done < <(head -n 100 "$1" 2>/dev/null)
    printf ']}]\n'
  } > "$out/lint.json"
  [ "${n:-0}" -eq 0 ]
}

run_python() {
  mapfile -t files < <(changed '\.py$')
  if [ "${#files[@]}" -gt 0 ]; then
    ruff check --output-format json "${files[@]}" > "$out/lint.json" 2>/dev/null; lint=$?
    bandit -f json -o "$out/qa.json" -q "${files[@]}" > /dev/null 2>&1 || true
  else
    echo '[]' > "$out/lint.json"; lint=0
  fi
  python -m pytest tests -q -p no:cacheprovider --junitxml="$out/junit.xml" --timeout=60 \
    > "$out/test.log" 2>&1; test=$?
}

run_node() {
  # Dependencies were installed into the image from the target repo's lockfile.
  [ -e node_modules ] || ln -s "$deps/node_modules" node_modules
  mapfile -t files < <(changed '\.(ts|tsx|js|jsx|mjs|cjs)$')
  echo '[]' > "$out/lint.json"; lint=0
  if [ "${#files[@]}" -gt 0 ]; then
    # Style lint uses the repo's own ESLint flat config; ESLint 9 fails outright without one, so skip it then.
    if compgen -G "eslint.config.*" > /dev/null; then
      "$fh/node_modules/.bin/eslint" -f json "${files[@]}" > "$out/lint.json" 2>/dev/null; lint=$?
    fi
    # Security scan always runs, with the factory's config (see eslint.security.mjs).
    "$fh/node_modules/.bin/eslint" --no-config-lookup -c "$fh/eslint.security.mjs" \
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
}

run_rust() {
  # Crates were vendored into the image from Cargo.lock; cargo reads them from there and never the network.
  export CARGO_HOME=/tmp/cargo CARGO_TARGET_DIR="$ws/target" CARGO_TERM_COLOR=never CARGO_NET_OFFLINE=true
  mkdir -p "$CARGO_HOME" && cp "$fh/cargo-config.toml" "$CARGO_HOME/config.toml"
  # Dependencies compiled when the image was built; copied in so a run compiles only the repo's own code.
  [ -d "$deps/target" ] && [ ! -e "$ws/target" ] && cp -a "$deps/target" "$ws/target"
  if [ ! -f Cargo.toml ]; then
    echo "factory: unsupported project: no Cargo.toml at the repo root" > "$out/test.log"; test=127; return
  fi
  mapfile -t files < <(changed '\.rs$')
  echo '[]' > "$out/lint.json"; lint=0
  if [ "${#files[@]}" -gt 0 ] && cargo clippy --version > /dev/null 2>&1; then
    cargo clippy --offline --all-targets --message-format=short > /dev/null 2> "$out/clippy.txt" || true
    printf '%s\n' "${files[@]}" | grep -F -f - "$out/clippy.txt" | grep -E ': error' > "$out/lint.txt" || true
    lint_json "$out/lint.txt"; lint=$?
    rm -f "$out/clippy.txt" "$out/lint.txt"
  fi
  if ! cargo nextest --version > /dev/null 2>&1; then
    echo "factory: cargo-nextest not installed in the runner image (rebuild it: make runner-rs)" \
      > "$out/test.log"; test=127; return
  fi
  cargo nextest run --offline --no-fail-fast --no-tests=warn --config-file "$fh/nextest.toml" \
    --profile factory > "$out/test.log" 2>&1; test=$?
  [ -f "$CARGO_TARGET_DIR/nextest/factory/junit.xml" ] && cp "$CARGO_TARGET_DIR/nextest/factory/junit.xml" "$out/junit.xml"
}

run_java() {
  if [ ! -f pom.xml ]; then
    echo "factory: unsupported build: only Maven projects (pom.xml) are supported; Gradle is not yet" \
      > "$out/test.log"; test=127; return
  fi
  echo '[]' > "$out/lint.json"; lint=0
  # The frozen tests live in tests/ (read-only). Maven compiles test sources from src/test/java, so they
  # are copied there under their own folder; the copy is the runner's, the frozen originals stay untouched.
  local dest=src/test/java/factory_frozen f
  rm -rf "$dest" && mkdir -p "$dest"
  while IFS= read -r f; do
    mkdir -p "$dest/$(dirname "$f")" && cp "tests/$f" "$dest/$f"
  done < <(cd tests 2>/dev/null && find . -name '*.java' -type f | sed 's|^\./||')
  # Dependencies were resolved into /opt/m2 when the image was built. It is read-only here, so Maven uses
  # it as a "tail" behind a scratch repository in /tmp, offline.
  mvn -B -o -ntp -Dmaven.repo.local=/tmp/m2 -Dmaven.repo.local.tail="$m2" \
    -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false test > "$out/test.log" 2>&1; test=$?
  if compgen -G "target/surefire-reports/TEST-*.xml" > /dev/null; then
    { echo '<testsuites>'
      for f in target/surefire-reports/TEST-*.xml; do sed -e 's/<?xml[^>]*?>//' "$f"; done
      echo '</testsuites>'; } > "$out/junit.xml"
  fi
}

run_godot() {
  if [ ! -f project.godot ]; then
    echo "factory: unsupported project: no project.godot at the repo root" > "$out/test.log"; test=127; return
  fi
  if [ ! -f addons/gut/gut_cmdln.gd ]; then
    echo "factory: GUT not found at addons/gut (commit the GUT addon to the repo)" > "$out/test.log"
    test=127; return
  fi
  mapfile -t files < <(changed '\.gd$')
  echo '[]' > "$out/lint.json"; lint=0
  if [ "${#files[@]}" -gt 0 ] && command -v gdlint > /dev/null; then
    gdlint "${files[@]}" 2>&1 | grep -E ':[0-9]+: Error:' > "$out/lint.txt" || true
    lint_json "$out/lint.txt"; lint=$?
    rm -f "$out/lint.txt"
  fi
  # Import assets first (writes .godot/ in the copy of the repo), then run GUT headless and quit.
  "$godot" --headless --path "$ws" --import > "$out/import.log" 2>&1 || true
  "$godot" --headless --path "$ws" -s res://addons/gut/gut_cmdln.gd -gdir=res://tests -ginclude_subdirs \
    -gprefix=test_ -gsuffix=.gd -gexit -gjunit_xml_file=res://.factory/junit.xml \
    > "$out/test.log" 2>&1; test=$?
  # GDScript fails at run time: a call to a missing function is a SCRIPT ERROR, and GUT still exits 0.
  [ "$test" -eq 0 ] && grep -q 'SCRIPT ERROR:' "$out/test.log" && test=1
  rm -f "$out/import.log"
}

runner="${FACTORY_RUNNER:-}"
if [ -z "$runner" ]; then
  if [ -f pyproject.toml ] || [ -f setup.py ] || compgen -G "tests/*.py" > /dev/null; then runner=python
  else runner=node; fi
fi
case "$runner" in
  python) run_python ;;
  node)   run_node ;;
  rust)   run_rust ;;
  java)   run_java ;;
  godot)  run_godot ;;
  *) echo "factory: unsupported runner '$runner'" > "$out/test.log"; test=127 ;;
esac

printf '{"runner":"%s","lint_exit":%d,"test_exit":%d}\n' "$runner" "$lint" "$test" > "$out/result.json"
exit $(( lint != 0 || test != 0 ))
