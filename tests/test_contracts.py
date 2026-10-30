"""Unit tests for the factory's own contracts. No Docker, Postgres or model calls needed.

Integration tests that need the running stack (breaker against Postgres, runner isolation, Langfuse
$0 cost) are listed in README "Acceptance checks" and run in Phase 1–2.
"""
import json
import pathlib
import subprocess

import pytest

from factory import config, graph, prompts
from factory.breaker import PatchRejected
from factory.gateway import Task, TaskKind, route
from factory.nodes import classify_failure, tree_sha256
from factory.patches import changed_files, extract_and_validate, validate_repo_path
from factory.pool import ModelSpec, match_id, norm
from factory.state import FailureClass, Patch, Plan, QAResult, SandboxResult, Tier


# ------------------------------------------------------------------ routing (D2, D4)
def msg(n_chars: int):
    return [{"role": "user", "content": "x" * n_chars}]


def test_plan_and_security_always_go_to_coding_lab():
    assert route(Task(TaskKind.PLAN, msg(10))).role == "plan"
    assert route(Task(TaskKind.SECURITY_FIX, msg(10))).role == "security_fix"


def test_assist_mode_keeps_all_code_on_claude():
    assert config.policy()["local_mode"] == "assist"
    d = route(Task(TaskKind.GENERATE, msg(100)))
    assert (d.tier, d.role) == (Tier.T3_REMOTE, "generate_remote")


def test_assist_mode_triages_locally():
    d = route(Task(TaskKind.TRIAGE_CLASSIFY, msg(100)))
    assert (d.tier, d.role) == (Tier.T2_LOCAL, "classify")


def test_large_context_and_multi_file_escalate():
    assert route(Task(TaskKind.GENERATE, msg(200_000))).tier is Tier.T3_REMOTE
    assert route(Task(TaskKind.GENERATE, msg(10), target_files=list("abcd"))).tier is Tier.T3_REMOTE


def test_generate_mode_routes_routine_work_locally(monkeypatch):
    p = dict(config.policy(), local_mode="generate")
    monkeypatch.setattr("factory.gateway.policy", lambda: p)
    assert route(Task(TaskKind.GENERATE, msg(100))).role == "generate"
    assert route(Task(TaskKind.REPAIR, msg(100), escalation_required=True)).role == "repair"


def test_off_mode_triages_with_cheapest_claude(monkeypatch):
    p = dict(config.policy(), local_mode="off")
    monkeypatch.setattr("factory.gateway.policy", lambda: p)
    d = route(Task(TaskKind.TRIAGE_CLASSIFY, msg(100)))
    assert (d.tier, d.role) == (Tier.T3_REMOTE, "spillover")


def test_coding_roles_are_anthropic_only():
    roles = config.policy()["roles"]
    for r in ("plan", "generate_remote", "repair", "security_fix", "spillover"):
        assert roles[r]["labs"] == ["anthropic"]


# ------------------------------------------------------------------ pool helpers (D2, D3)
def test_match_id_accepts_dated_provider_ids_and_prefers_exact():
    ids = {norm(i): i for i in ["claude-x-4-5-20250101", "claude-x-4-5-20251001", "claude-x-4"]}
    assert match_id(norm("claude-x-4.5"), ids) == "claude-x-4-5-20251001"
    assert match_id(norm("claude-x-4"), ids) == "claude-x-4"          # not swallowed by 4-5
    assert match_id(norm("claude-y-1"), ids) is None


def test_subscription_calls_cost_zero_but_keep_list_price():
    s = ModelSpec("anthropic/m", "m", "anthropic_subscription", "t3", 3.0, 15.0, 200_000, billed=False)
    assert s.cost(1_000_000, 1_000_000) == 0.0
    assert s.list_cost(1_000_000, 1_000_000) == pytest.approx(18.0)


def test_subscription_adapter_contract(monkeypatch):
    """Real ClaudeAgentOptions (validates our option names); stubbed query() so no CLI or network."""
    import claude_agent_sdk as sdk

    from factory.pool import QuotaExhausted, SubscriptionAdapter

    class Text:
        def __init__(self, text): self.text = text

    class Assistant:
        def __init__(self, content): self.content = content

    class Result:
        def __init__(self, is_error=False, result="", usage=None):
            self.is_error, self.result, self.usage, self.total_cost_usd = is_error, result, usage, 0.42

    seen = {}
    script = {"msgs": []}

    async def fake_query(prompt, options):
        seen["options"], seen["prompt"] = options, prompt
        for m in script["msgs"]:
            yield m

    for name, val in (("query", fake_query), ("AssistantMessage", Assistant), ("ResultMessage", Result), ("TextBlock", Text)):
        monkeypatch.setattr(sdk, name, val)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    a = SubscriptionAdapter()
    script["msgs"] = [Assistant([Text("```diff\n...\n```")]),
                      Result(usage={"input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 20})]
    r = a.chat.completions.create(model="claude-x-4-5", max_tokens=100,
                                  messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
    o = seen["options"]
    assert o.tools == [] and o.max_turns == 1 and o.setting_sources == [] and "Bash" in o.disallowed_tools
    assert o.system_prompt == "S" and seen["prompt"] == "U"
    assert r.choices[0].message.content.startswith("```diff")
    assert (r.usage.prompt_tokens, r.usage.completion_tokens) == (150, 20)

    script["msgs"] = [Result(is_error=True, result="Claude usage limit reached. Resets at 5pm")]
    with pytest.raises(QuotaExhausted):
        a.chat.completions.create(model="m", max_tokens=10, messages=[{"role": "user", "content": "U"}])

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-set")
    with pytest.raises(SystemExit):
        SubscriptionAdapter()


def test_breaker_worst_case_example_from_spec():
    s = ModelSpec("x/m", "m", "anthropic", "t3", 2.0, 12.0, 200_000)
    assert s.cost(14_000, 8_192) == pytest.approx(0.126, abs=1e-3)


# ------------------------------------------------------------------ patches (V4, R10)
@pytest.fixture()
def repo(tmp_path: pathlib.Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                   check=True)
    return tmp_path


GOOD = """```diff
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
```"""


def test_valid_diff_passes(repo):
    diff = extract_and_validate(GOOD, repo)
    assert changed_files(diff) == ["app.py"]


def test_patch_touching_frozen_tests_is_rejected(repo):
    bad = GOOD.replace("a/app.py", "a/tests/test_app.py").replace("b/app.py", "b/tests/test_app.py")
    with pytest.raises(PatchRejected):
        extract_and_validate(bad, repo)


def test_patch_touching_lockfile_or_ci_is_rejected(repo):
    for path in ("uv.lock", "package-lock.json", ".github/workflows/ci.yml", "requirements.txt"):
        bad = GOOD.replace("a/app.py", f"a/{path}").replace("b/app.py", f"b/{path}")
        with pytest.raises(PatchRejected):
            extract_and_validate(bad, repo)


def test_malformed_diff_is_rejected(repo):
    with pytest.raises(PatchRejected):
        extract_and_validate("here is the fix: return a + b", repo)
    with pytest.raises(PatchRejected):
        extract_and_validate(GOOD.replace("return a - b", "return a * b"), repo)   # does not apply


@pytest.mark.parametrize("p", ["../etc/passwd", "/abs/path", ".git/config", "a/../../b"])
def test_unsafe_paths_rejected(p):
    with pytest.raises(PatchRejected):
        validate_repo_path(p)


# ------------------------------------------------------------------ FSM routers (§1.3, §4.2)
def sb(fc, ok=True):
    return SandboxResult(exit_code=0 if fc is FailureClass.NONE else 1, passed=1, failed=0, lint_errors=0,
                         failure_class=fc, tests_hash_ok=ok)


def test_halt_reason_short_circuits_every_router():
    s = {"halt_reason": "breaker: cap", "accepted": True, "sandbox": sb(FailureClass.NONE),
         "qa": QAResult(high_findings=0, medium_findings=0)}
    for r in (graph.route_after_triage, graph.route_after_generate, graph.route_after_sandbox,
              graph.route_after_qa, graph.route_after_repair, graph.route_after_pr):
        assert r(s) == "halt"
    assert graph.next_or_halt("generate")(s) == "halt"


def test_tamper_and_environment_never_reach_repair():
    assert graph.route_after_sandbox({"sandbox": sb(FailureClass.TAMPER, ok=False)}) == "halt"
    assert graph.route_after_sandbox({"sandbox": sb(FailureClass.ENVIRONMENT)}) == "halt"


def test_fourth_repair_is_refused():
    s = {"sandbox": sb(FailureClass.ASSERTION), "repair_attempts": 3}
    assert graph.route_after_sandbox(s) == "halt"
    s["repair_attempts"] = 2
    assert graph.route_after_sandbox(s) == "repair"


def test_rejected_generate_goes_to_repair_not_sandbox():
    assert graph.route_after_generate({"patch_rejected": True, "repair_attempts": 1}) == "repair"
    assert graph.route_after_generate({"patch_rejected": False}) == "sandbox"


def test_qa_high_findings_go_to_security_fix():
    plan = Plan(summary="s", target_files=["app.py"], acceptance_criteria=["c"], test_files=[], complexity="routine")
    s = {"qa": QAResult(high_findings=1, medium_findings=0), "plan": plan,
         "patch": Patch(unified_diff="", produced_by=Tier.T3_REMOTE, model="m")}
    assert graph.route_after_qa(s) == "repair"
    assert prompts.next_repair_role(s) == (Tier.T3_REMOTE, "security_fix")


def test_local_syntax_failure_gets_one_local_retry_then_escalates():
    s = {"sandbox": sb(FailureClass.SYNTAX), "repair_attempts": 0,
         "patch": Patch(unified_diff="", produced_by=Tier.T2_LOCAL, model="local/m")}
    assert prompts.next_repair_role(s)[0] is Tier.T2_LOCAL
    s["repair_attempts"] = 1
    assert prompts.next_repair_role(s)[0] is Tier.T3_REMOTE
    s.update(repair_attempts=0, sandbox=sb(FailureClass.ASSERTION))
    assert prompts.next_repair_role(s)[0] is Tier.T3_REMOTE


# ------------------------------------------------------------------ sandbox result parsing (§1b.5)
def test_failure_classification(tmp_path):
    out = tmp_path
    (out / "test.log").write_text("E   ModuleNotFoundError: No module named 'requests'")
    assert classify_failure(out, {"exit_code": 1})[0] is FailureClass.ENVIRONMENT
    (out / "test.log").write_text("E   AssertionError")
    (out / "junit.xml").write_text('<testsuites><testsuite tests="3" failures="1" errors="0" skipped="0"/></testsuites>')
    fc, passed, failed, _, _ = classify_failure(out, {"exit_code": 1})
    assert (fc, passed, failed) == (FailureClass.ASSERTION, 2, 1)
    assert classify_failure(out, {"timed_out": True})[0] is FailureClass.TIMEOUT
    (out / "junit.xml").write_text('<testsuites><testsuite tests="3" failures="0" errors="0" skipped="0"/></testsuites>')
    (out / "test.log").write_text("3 passed")
    (out / "lint.json").write_text(json.dumps([{"code": "F401", "message": "unused import"}]))
    assert classify_failure(out, {"exit_code": 1})[0] is FailureClass.SYNTAX
    (out / "lint.json").write_text("[]")
    assert classify_failure(out, {"exit_code": 0})[0] is FailureClass.NONE


def test_harness_errors_are_environment_not_model_failures(tmp_path):
    (tmp_path / "test.log").write_text("ERROR: usage: python -m pytest ... unrecognized arguments")
    for code in (3, 4, 5):
        (tmp_path / "result.json").write_text(json.dumps({"lint_exit": 0, "test_exit": code}))
        assert classify_failure(tmp_path, {"exit_code": 1})[0] is FailureClass.ENVIRONMENT


def test_bandit_high_findings_block_the_pr(tmp_path):
    from factory.nodes import qa

    class D:
        def repo_path(self, s):
            return tmp_path
    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory" / "qa.json").write_text(json.dumps({"results": [
        {"filename": "app.py", "line_number": 3, "issue_severity": "HIGH", "issue_text": "subprocess shell=True"},
        {"filename": "app.py", "line_number": 9, "issue_severity": "LOW", "issue_text": "assert used"}]}))
    r = qa({}, D())["qa"]
    assert (r.high_findings, r.medium_findings) == (1, 0)
    assert graph.route_after_qa({"qa": r, "repair_attempts": 0}) == "repair"


def test_tree_hash_detects_any_change(tmp_path):
    (tmp_path / "t.py").write_text("a")
    h = tree_sha256(tmp_path)
    (tmp_path / "t.py").write_text("b")
    assert tree_sha256(tmp_path) != h


def test_repair_prompt_splits_stable_prefix_for_caching():
    plan = Plan(summary="s", target_files=["app.py"], acceptance_criteria=["c"], test_files=["tests/t.py"],
                test_sources={"tests/t.py": "x"}, complexity="routine")
    from factory.state import TicketSpec
    s = {"ticket": TicketSpec(ticket_id="T-1", title="t", body="b", repo="o/r", language="python"),
         "plan": plan, "sandbox": sb(FailureClass.ASSERTION),
         "patch": Patch(unified_diff="d1", produced_by=Tier.T3_REMOTE, model="m")}
    m1 = prompts.repair(s, {"app.py": "src"}, [])
    s["patch"] = Patch(unified_diff="d2", produced_by=Tier.T3_REMOTE, model="m")
    m2 = prompts.repair(s, {"app.py": "src"}, [{"attempt": 1}])
    assert m1[:2] == m2[:2] and m1[2] != m2[2]


# ------------------------------------------------------------------ Node runner path (hello-node)
def test_traceback_paths_are_mapped_to_repo_relative(tmp_path):
    from factory.nodes import mentioned_files
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "greet.js").write_text("x")
    log = (f"at greet ({tmp_path}/src/greet.js:9:3)\n  at /workspace/src/greet.js:2\n"
           "  at node_modules/jest-circus/build/x.js:1\n  /etc/passwd.js  ../../escape.js  ./src/greet.js")
    assert mentioned_files(tmp_path, log) == ["src/greet.js"]


@pytest.mark.parametrize("log", [
    "factory: jest not installed in the runner image (add it to devDependencies, then make runner-ts)",
    "No tests found, exiting with code 1\nRun with `--passWithNoTests` to exit with code 0",
    "Cannot find module 'lodash' from 'src/greet.js'",
])
def test_node_harness_problems_halt_instead_of_paid_repair(tmp_path, log):
    (tmp_path / "test.log").write_text(log)
    assert classify_failure(tmp_path, {"exit_code": 1})[0] is FailureClass.ENVIRONMENT


def test_missing_relative_module_is_a_code_failure(tmp_path):
    (tmp_path / "test.log").write_text("Cannot find module '../src/farewell' from 'tests/farewell.test.js'")
    (tmp_path / "jest.json").write_text(json.dumps({"numPassedTests": 2, "numFailedTests": 0,
                                                    "numRuntimeErrorTestSuites": 1}))
    assert classify_failure(tmp_path, {"exit_code": 1})[0] is FailureClass.ASSERTION


def test_eslint_security_errors_block_and_warnings_do_not(tmp_path):
    from factory.nodes import qa

    class D:
        def repo_path(self, s):
            return tmp_path
    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory" / "qa.json").write_text(json.dumps([{"filePath": "/workspace/src/greet.js", "messages": [
        {"ruleId": "no-eval", "severity": 2, "line": 3, "message": "eval can be harmful."},
        {"ruleId": "security/detect-object-injection", "severity": 1, "line": 5, "message": "Generic Object Injection Sink"}]}]))
    r = qa({}, D())["qa"]
    assert (r.high_findings, r.medium_findings) == (1, 1)


def test_javascript_is_a_valid_language():
    from factory.github import validate
    from factory.state import TicketSpec
    assert TicketSpec(ticket_id="t", title="t", body="b", repo="o/r", language="javascript").language == "javascript"
    assert validate({"repos": [{"repo": "o/r", "language": "javascript"}]})["repos"][0]["language"] == "javascript"


def test_hello_node_example_is_consistent():
    root = pathlib.Path(__file__).resolve().parent.parent / "examples" / "hello-node"
    pkg = json.loads((root / "package.json").read_text())
    lock = json.loads((root / "package-lock.json").read_text())
    assert {"jest", "eslint"} <= set(pkg["devDependencies"]) and lock["packages"][""]["devDependencies"] == pkg["devDependencies"]
    assert (root / "eslint.config.js").exists() and (root / "tests" / "greet.test.js").exists()
    assert "node_modules" in (root / ".gitignore").read_text()
