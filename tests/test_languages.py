"""Runner families beyond Python and Node: Rust, Java (Maven) and Godot (GDScript with GUT).

The fixtures named red-nextest-*, red-gut-* and red-cargo-* are real output from the runner entrypoint,
run against examples/hello-rust and examples/hello-godot with cargo-nextest 0.9, Godot 4.4.1 and GUT 9.4.
The Surefire report below is written by hand in Surefire's format: Maven Central was not reachable when
these tests were written.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from factory import depshash, signatures
from factory.nodes import _parse_results, classify_failure, new_test_outcome
from factory.patches import FORBIDDEN
from factory.state import FailureClass, TicketSpec

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
ENTRY = ROOT / "runner-images" / "entrypoint.sh"


def results(tmp_path, **files) -> pathlib.Path:
    for name, text in files.items():
        (tmp_path / name.replace("_", ".", 1)).write_text(text)
    return tmp_path


# ------------------------------------------------------------------ languages and families
@pytest.mark.parametrize("lang,fam", [("python", "python"), ("node", "node"), ("javascript", "node"),
                                      ("typescript", "node"), ("rust", "rust"), ("java", "java"),
                                      ("godot", "godot")])
def test_every_language_has_a_runner_family(lang, fam):
    from factory.github import validate
    assert depshash.family(lang) == fam
    assert TicketSpec(ticket_id="t", title="t", body="b", repo="o/r", language=lang).language == lang
    assert validate({"repos": [{"repo": "o/r", "language": lang}]})["repos"][0]["language"] == lang


def test_unknown_language_is_refused_everywhere():
    from factory.github import validate
    with pytest.raises(ValueError):
        depshash.family("go")
    with pytest.raises(Exception):
        TicketSpec(ticket_id="t", title="t", body="b", repo="o/r", language="go")
    with pytest.raises(SystemExit):
        validate({"repos": [{"repo": "o/r", "language": "go"}]})


def test_broker_knows_an_image_for_every_language():
    src = (ROOT / "sandbox_broker" / "app.py").read_text()
    for lang in depshash.LANGUAGES:
        assert f'"{lang}":' in src, lang


def test_migration_allows_every_language():
    sql = (ROOT / "orchestrator" / "migrations" / "007_languages.sql").read_text()
    for lang in depshash.LANGUAGES:
        assert f"'{lang}'" in sql


@pytest.mark.parametrize("lang,files,expected", [
    ("rust", ["Cargo.toml", "Cargo.lock", "build.rs"], ["Cargo.lock", "Cargo.toml"]),
    ("java", ["pom.xml", "build.gradle"], ["pom.xml"]),
    ("godot", [".godot-version", "project.godot"], [".godot-version"]),
    ("node", ["package.json", "package-lock.json", "yarn.lock"], ["package-lock.json", "package.json"]),
    ("python", ["requirements.txt", "requirements-dev.txt", "pyproject.toml"],
     ["requirements-dev.txt", "requirements.txt"]),
])
def test_dependency_files_per_family(tmp_path, lang, files, expected):
    for f in files:
        (tmp_path / f).write_text(f)
    assert depshash.deps_files(tmp_path, lang) == expected


def test_node_aliases_share_one_image_and_old_hashes_stay_valid(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    hashes = {depshash.deps_hash(tmp_path, lang) for lang in ("node", "javascript", "typescript")}
    assert len(hashes) == 1
    assert depshash.deps_hash(tmp_path, "rust") not in hashes
    import hashlib                                              # the hash the previous release computed
    old = hashlib.sha256(b"lang:node\0")
    old.update(b"package.json\0{}\0")
    assert hashes == {old.hexdigest()}


def test_godot_version_change_means_a_rebuild(tmp_path):
    (tmp_path / ".godot-version").write_text("4.3\n")
    a = depshash.deps_hash(tmp_path, "godot")
    (tmp_path / ".godot-version").write_text("4.4.1\n")
    assert depshash.deps_hash(tmp_path, "godot") != a


@pytest.mark.parametrize("path", ["Cargo.toml", "Cargo.lock", "crates/a/Cargo.toml", "pom.xml", "build.gradle",
                                  "settings.gradle.kts", ".godot-version", "addons/gut/gut.gd"])
def test_patches_cannot_touch_new_manifests_or_the_test_framework(path):
    assert FORBIDDEN.search(f"+++ b/{path}")


@pytest.mark.parametrize("path", ["src/lib.rs", "src/main/java/a/B.java", "scripts/player.gd", "project.godot"])
def test_source_files_stay_patchable(path):
    assert not FORBIDDEN.search(f"+++ b/{path}")


# ------------------------------------------------------------------ signatures: bodies never shown
def test_rust_signatures_hide_bodies_and_private_items():
    src = ("pub fn add(a: i32, b: i32) -> i32 {\n    let secret = 42;\n    a + b + secret\n}\n"
           "fn private_helper() {}\npub struct Point { pub x: i32 }\nimpl Point {\n"
           "    pub fn new(x: i32) -> Self { Self { x } }\n}\n")
    out = signatures.rust_signatures(src)
    assert "pub fn add(a: i32, b: i32) -> i32" in out and "pub fn new(x: i32) -> Self" in out
    assert "secret" not in out and "private_helper" not in out and "a + b" not in out


def test_java_signatures_hide_bodies_and_private_members():
    src = ("package com.acme;\npublic class Greeter {\n    public static final String HELLO = \"hi\";\n"
           "    private int n;\n    public Greeter(int n) { this.n = n; }\n"
           "    public String greet(String name) {\n        return HELLO + name;\n    }\n"
           "    private void secret() {}\n}\n")
    out = signatures.java_signatures(src)
    assert "public String greet(String name)" in out and "public Greeter(int n)" in out
    assert "package com.acme;" in out
    assert "secret" not in out and "return" not in out and '"hi"' not in out


def test_gdscript_signatures_hide_bodies_and_private_functions():
    src = ("extends Node2D\nclass_name Player\nsignal died(reason)\nconst SPEED := 200.0\n"
           "func take_damage(amount: int) -> void:\n\thealth -= amount\n\tdied.emit(\"hp\")\n"
           "func _ready():\n\tpass\n")
    out = signatures.gdscript_signatures(src)
    assert "func take_damage(amount: int) -> void" in out and "class_name Player" in out
    assert "signal died(reason)" in out
    assert "_ready" not in out and "health -=" not in out and "200.0" not in out


def test_signature_reader_is_picked_by_extension(tmp_path):
    (tmp_path / "lib.rs").write_text("pub fn a() -> u8 { 1 }\n")
    (tmp_path / "A.java").write_text("public class A {\n    public int b() { return 2; }\n}\n")
    (tmp_path / "c.gd").write_text("func c() -> int:\n\treturn 3\n")
    out = signatures.signatures(tmp_path, ["lib.rs", "A.java", "c.gd"], "rust")
    assert out == {"lib.rs": "pub fn a() -> u8", "A.java": "public class A\n    public int b()",
                   "c.gd": "func c() -> int"}


def test_java_test_style_reads_existing_tests_from_src_test_java(tmp_path):
    t = tmp_path / "src" / "test" / "java" / "com" / "x"
    t.mkdir(parents=True)
    (t / "GreeterTest.java").write_text("package com.x;\nimport org.junit.jupiter.api.Test;\n")
    assert "org.junit.jupiter" in signatures.test_style(tmp_path, "java")


def test_test_writer_is_told_each_frameworks_file_rules():
    from factory import prompts
    from factory.state import Plan
    plan = Plan(summary="s", target_files=["a"], acceptance_criteria=["c"], complexity="trivial")
    for lang, words in (("rust", "tests/<name>.rs"), ("java", "tests/<Name>Test.java"),
                        ("godot", "extends GutTest"), ("node", "Jest"), ("python", "pytest")):
        t = TicketSpec(ticket_id="t", title="t", body="b", repo="o/r", language=lang)
        assert words in prompts.write_tests(t, plan, {}, "")[0]["content"]


# ------------------------------------------------------------------ reading results: Rust (nextest)
def test_nextest_junit_counts_new_rust_tests(tmp_path):
    res = results(tmp_path, junit_xml=(FIX / "red-nextest-junit.xml").read_text())
    assert new_test_outcome(res, ["tests/shout.rs"]) == (1, 1)
    assert new_test_outcome(res, ["tests/greet.rs"]) == (2, 0)
    assert _parse_results(res) == (2, 1)


def test_rust_test_that_calls_missing_code_fails_the_red_check(tmp_path):
    """The new test imports a function that does not exist yet: cargo stops and writes no report."""
    res = results(tmp_path, test_log=(FIX / "red-cargo-compile.log").read_text(),
                  result_json=json.dumps({"runner": "rust", "lint_exit": 0, "test_exit": 101}))
    assert new_test_outcome(res, ["tests/shout.rs"]) == (1, 1)
    assert classify_failure(res, {"exit_code": 1})[0] is FailureClass.SYNTAX      # repairable, not environment


def test_other_compile_errors_do_not_count_as_a_red_test(tmp_path):
    res = results(tmp_path, test_log="error: expected one of `,`, `:`, or `}`, found `{`\n --> tests/shout.rs:3:1\n")
    assert new_test_outcome(res, ["tests/shout.rs"]) == (0, 0)                   # back to the test writer


@pytest.mark.parametrize("log", [
    "error: no matching package named `itoa` found\nlocation searched: crates.io index",
    "error: failed to select a version for the requirement `serde = \"^9\"`",
    "error: attempting to make an HTTP request, but --offline was specified",
    "factory: cargo-nextest not installed in the runner image (rebuild it: make runner-rs)",
    "factory: unsupported project: no Cargo.toml at the repo root",
])
def test_rust_harness_problems_halt_instead_of_paid_repair(tmp_path, log):
    res = results(tmp_path, test_log=log, result_json=json.dumps({"runner": "rust", "test_exit": 101}))
    assert classify_failure(res, {"exit_code": 1})[0] is FailureClass.ENVIRONMENT


def test_cargo_exit_codes_are_not_read_as_pytest_codes(tmp_path):
    """nextest exits 4 when no tests match; for pytest that would mean a usage error (environment)."""
    res = results(tmp_path, test_log="error: no tests to run", junit_xml=(FIX / "red-nextest-junit.xml").read_text(),
                  result_json=json.dumps({"runner": "rust", "lint_exit": 0, "test_exit": 4}))
    assert classify_failure(res, {"exit_code": 1})[0] is FailureClass.ASSERTION


# ------------------------------------------------------------------ reading results: Godot (GUT)
def test_gut_test_that_never_asserted_is_a_failure_not_a_pass(tmp_path):
    """Calling a missing function is a runtime error in GDScript; GUT then reports the test as passing
    with status "no asserts". That must not count as a pass, in the red check or in the sandbox."""
    res = results(tmp_path, junit_xml=(FIX / "red-gut-junit.xml").read_text())
    assert new_test_outcome(res, ["tests/test_shout.gd"]) == (1, 1)
    assert _parse_results(res) == (2, 1)


def test_gut_failed_assertion_is_counted_once(tmp_path):
    res = results(tmp_path, junit_xml=(FIX / "red-gut-junit-failed.xml").read_text())
    assert new_test_outcome(res, ["tests/test_shout.gd"]) == (1, 1)
    assert new_test_outcome(res, ["tests/test_greeter.gd"]) == (2, 0)
    assert _parse_results(res) == (2, 1)


@pytest.mark.parametrize("log", [
    "factory: GUT not found at addons/gut (commit the GUT addon to the repo)",
    "factory: unsupported project: no project.godot at the repo root",
])
def test_godot_harness_problems_halt_instead_of_paid_repair(tmp_path, log):
    res = results(tmp_path, test_log=log, result_json=json.dumps({"runner": "godot", "test_exit": 127}))
    assert classify_failure(res, {"exit_code": 1})[0] is FailureClass.ENVIRONMENT


def test_gdscript_parse_error_is_repairable(tmp_path):
    res = results(tmp_path, test_log='SCRIPT ERROR: Parse Error: Expected ":" after function declaration.\n',
                  result_json=json.dumps({"runner": "godot", "test_exit": 1}))
    assert classify_failure(res, {"exit_code": 1})[0] is FailureClass.SYNTAX


# ------------------------------------------------------------------ reading results: Java (Maven Surefire)
SUREFIRE = """<testsuites>
<testsuite name="com.example.GreeterTest" tests="2" errors="0" skipped="0" failures="0">
  <testcase name="greetsByName" classname="com.example.GreeterTest" time="0.01"/>
  <testcase name="greetsTheWorldByDefault" classname="com.example.GreeterTest" time="0"/>
</testsuite>
<testsuite name="com.example.ShoutTest" tests="1" errors="0" skipped="0" failures="1">
  <testcase name="shoutsTheName" classname="com.example.ShoutTest" time="0.01">
    <failure message="expected: &lt;HELLO, ADA!&gt; but was: &lt;Ada&gt;" type="org.opentest4j.AssertionFailedError"/>
  </testcase>
</testsuite>
</testsuites>"""

JAVAC_MISSING = """[INFO] --- compiler:3.13.0:testCompile (default-testCompile) @ hello-factory ---
[ERROR] COMPILATION ERROR :
[ERROR] /workspace/src/test/java/factory_frozen/ShoutTest.java:[9,41] cannot find symbol
  symbol:   method shout(java.lang.String)
  location: class com.example.Greeter
[INFO] BUILD FAILURE"""


def test_surefire_report_counts_new_java_tests_by_class(tmp_path):
    res = results(tmp_path, junit_xml=SUREFIRE)
    assert new_test_outcome(res, ["tests/ShoutTest.java"]) == (1, 1)
    assert new_test_outcome(res, ["tests/GreeterTest.java"]) == (2, 0)
    assert _parse_results(res) == (2, 1)


def test_java_test_that_calls_missing_code_fails_the_red_check(tmp_path):
    res = results(tmp_path, test_log=JAVAC_MISSING, result_json=json.dumps({"runner": "java", "test_exit": 1}))
    assert new_test_outcome(res, ["tests/ShoutTest.java"]) == (1, 1)
    assert classify_failure(res, {"exit_code": 1})[0] is FailureClass.SYNTAX


def test_java_offline_resolution_failure_halts(tmp_path):
    """Real Maven output from the Java runner with no dependencies resolved into the image."""
    log = ("[ERROR] Plugin org.apache.maven.plugins:maven-resources-plugin:3.3.1 or one of its dependencies "
           "could not be resolved:\n[ERROR] \tCannot access central (https://repo.maven.apache.org/maven2) in "
           "offline mode and the artifact org.apache.maven.plugins:maven-resources-plugin:jar:3.3.1 has not "
           "been downloaded from it before.")
    res = results(tmp_path, test_log=log, result_json=json.dumps({"runner": "java", "test_exit": 1}))
    assert classify_failure(res, {"exit_code": 1})[0] is FailureClass.ENVIRONMENT


def test_repair_sees_rust_java_and_gdscript_files_named_in_logs(tmp_path):
    from factory.nodes import mentioned_files
    for f in ("src/lib.rs", "src/main/java/com/x/Greeter.java", "scripts/greeter.gd"):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x")
    log = (" --> src/lib.rs:3:5\n[ERROR] /workspace/src/main/java/com/x/Greeter.java:[4,1]\n"
           "at: shout (res://scripts/greeter.gd:9)\n --> target/debug/build/x.rs\n res://addons/gut/gut.gd:1")
    assert mentioned_files(tmp_path, log) == ["scripts/greeter.gd", "src/lib.rs", "src/main/java/com/x/Greeter.java"]


# ------------------------------------------------------------------ the entrypoint itself
def _entry(tmp_path, runner: str, env: dict | None = None) -> dict:
    (tmp_path / ".factory").mkdir(exist_ok=True)
    p = subprocess.run(["bash", str(ENTRY)], capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "FACTORY_WORKSPACE": str(tmp_path), "FACTORY_RUNNER": runner,
                            **(env or {})})
    return {"exit": p.returncode, **json.loads((tmp_path / ".factory" / "result.json").read_text()),
            "log": (tmp_path / ".factory" / "test.log").read_text()}


@pytest.mark.parametrize("runner,needs,message", [
    ("rust", None, "no Cargo.toml"),
    ("java", None, "only Maven projects"),
    ("godot", None, "no project.godot"),
    ("godot", "project.godot", "GUT not found"),
    ("cobol", None, "unsupported runner"),
])
def test_entrypoint_reports_missing_project_files_as_environment(tmp_path, runner, needs, message):
    if needs:
        (tmp_path / needs).write_text("")
    r = _entry(tmp_path, runner, {"FACTORY_HOME": str(tmp_path)})
    assert r["exit"] == 1 and r["runner"] == runner and r["test_exit"] == 127 and message in r["log"]
    assert classify_failure(tmp_path / ".factory", {"exit_code": 1})[0] is FailureClass.ENVIRONMENT


def test_entrypoint_lint_json_escapes_hostile_text(tmp_path):
    """A lint message can quote the model's code; lint.json must stay valid JSON whatever it contains."""
    lines = tmp_path / "lint.txt"
    lines.write_text('src/lib.rs:1:1: error: "quoted" \\ back\tslash \x07bell\n')
    script = (f'out={tmp_path}; ' + ENTRY.read_text().split("run_python() {")[0].split("lint=0; test=0", 1)[1]
              + f'lint_json {lines}; echo $?')
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    data = json.loads((tmp_path / "lint.json").read_text())
    assert p.stdout.strip() == "1" and data[0]["errorCount"] == 1
    assert data[0]["messages"][0]["message"] == 'src/lib.rs:1:1: error: "quoted" \\ back\tslash bell'.replace("\t", "")


def test_every_runner_image_declares_its_family():
    for d, fam in (("python", "python"), ("typescript", "node"), ("rust", "rust"), ("java", "java"),
                   ("godot", "godot")):
        assert f"ENV FACTORY_RUNNER={fam}" in (ROOT / "runner-images" / d / "Dockerfile").read_text()


def test_examples_exist_for_each_new_family():
    ex = ROOT / "examples"
    assert (ex / "hello-rust" / "Cargo.toml").exists() and (ex / "hello-rust" / "tests" / "greet.rs").exists()
    assert (ex / "hello-java" / "pom.xml").exists()
    assert (ex / "hello-godot" / "project.godot").exists() and (ex / "hello-godot" / ".godot-version").exists()
