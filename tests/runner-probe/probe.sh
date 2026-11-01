#!/bin/sh
# Fake runner for `make test-sandbox`: probes its own sandbox, then plants every result-file trick from
# the security review (symlinked log, oversized lint file, nested file). The broker must drop them all.
out=/workspace/.factory
net=blocked
if command -v nc > /dev/null 2>&1; then nc -w 2 1.1.1.1 80 < /dev/null > /dev/null 2>&1 && net=OPEN
else (exec 3<>/dev/tcp/1.1.1.1/80) 2> /dev/null && net=OPEN; fi
git=absent;   [ -e /workspace/.git ] && git=PRESENT
tests_ro=yes; (echo x > /workspace/tests/pwn.txt) 2> /dev/null && tests_ro=NO
src=missing;  [ -f /workspace/app.js ] && src=ok
link=absent;  [ -L /workspace/evil-link ] && link=PRESENT
frozen=missing; [ -f /workspace/tests/a.test.js ] && frozen=ok
ln -s /etc/passwd "$out/test.log"
head -c 2000000 /dev/zero > "$out/lint.json"
mkdir -p "$out/sub" && echo nested > "$out/sub/qa.json"
changed=$(cat "$out/changed_files.txt")
printf '{"net":"%s","git":"%s","tests_ro":"%s","src":"%s","link":"%s","frozen":"%s","changed":"%s"}' \
  "$net" "$git" "$tests_ro" "$src" "$link" "$frozen" "$changed" > "$out/result.json"
echo '<testsuite tests="1" failures="0"/>' > "$out/junit.xml"
echo "probe done"
