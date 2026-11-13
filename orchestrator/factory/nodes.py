"""FSM nodes (spec §1.3). Each takes (state, deps) and returns a partial state update."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import pathlib
import re
import shutil
import stat
import subprocess
import xml.etree.ElementTree as ET

import httpx

from factory import criteria, depshash, patches, projects, prompts, review, signatures
from factory.breaker import PatchRejected, alert
from factory.config import is_factory_repo, redact
from factory.gateway import AUTHOR_ROLES, Task, TaskKind
from factory.obs import trace_url
from factory.state import (FailureClass, Patch, Plan, QAResult, RedCheck, SandboxResult, Tier, TicketSpec)

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]{1,100}$")
REJECT_RE = re.compile(r"(?i)\b(migrations?|terraform|kubernetes|helm|secrets?|credentials?|passwords?|"
                       r"api[ _-]?keys?|\.github/workflows|ci pipeline|infrastructure|dockerfile)\b")
SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+")
RESULT_FILES = {"lint.json", "qa.json", "junit.xml", "jest.json", "test.log", "result.json"}
TAG_RE = re.compile(r"<[^>]+>")
JSON_RE = re.compile(r"\{.*\}", re.S)


def _json(text: str) -> dict:
    m = JSON_RE.search(text or "")
    if not m:
        raise ValueError("no JSON object in model output")
    return json.loads(m.group(0))


# ------------------------------------------------------------------ INGEST (T1)
def ingest(state, deps):
    t: TicketSpec = state["ticket"]
    if not REPO_RE.fullmatch(t.repo) or not BRANCH_RE.fullmatch(t.base_branch):
        return {"halt_reason": f"invalid repo or branch: {t.repo}@{t.base_branch}"}
    body = TAG_RE.sub("", html.unescape(t.body))[:16_000]
    found = criteria.parse(body)
    ticket = t.model_copy(update={"body": body, "title": t.title[:300],
                                  "criteria": [redact(c) for c in found.testable],
                                  "manual_checks": [redact(c) for c in found.manual]})
    if is_factory_repo(t.repo):
        return {"halt_reason": f"refused: {t.repo} is the factory's own repo. Change the factory by "
                               "pull request and `make up`, never through a factory run."}
    project = projects.get(deps.dsn, t.repo) if getattr(deps, "dsn", None) else None
    if not state.get("qualification"):
        if project is None:
            return {"halt_reason": f"refused: {t.repo} is not a registered project. Add it with "
                                   f"make project-add REPO={t.repo} LANGUAGE=<language>"}
        if project.status != "active":
            return {"halt_reason": f"refused: project {t.repo} is paused (make project-resume REPO={t.repo})"}
        if not project.runner_image:
            return {"halt_reason": f"sandbox environment: {t.repo} has no runner image yet. "
                                   f"Run: make project-rebuild REPO={t.repo}"}
        # The project record is authoritative for language and branch, whatever the ticket file says.
        t = ticket = ticket.model_copy(update={"language": project.language, "base_branch": project.base_branch})
    repo = deps.repo_path(state)
    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", *patches.GIT_HARDEN, "clone", "--depth", "50", "--branch", t.base_branch,
                            f"https://github.com/{t.repo}.git", str(repo)],
                           capture_output=True, env=patches.git_env(deps.git_auth_env()))
        if r.returncode != 0:
            return {"halt_reason": "clone failed: " + r.stderr.decode(errors="replace")[-500:]}
    sha = patches.git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    if project is not None and not state.get("qualification") \
            and depshash.deps_hash(repo, project.language) != project.deps_hash:
        return {"halt_reason": f"sandbox environment: {t.repo} dependencies changed since its runner image was "
                               f"built. Run: make project-rebuild REPO={t.repo}, then retry"}
    return {"ticket": ticket, "base_sha": sha, "runner_image": project.runner_image if project else None}


# ------------------------------------------------------------------ TRIAGE (T1 -> local)
def triage(state, deps):
    if state.get("qualification"):
        return {"accepted": True, "triage_reason": "qualification run"}
    t = state["ticket"]
    m = REJECT_RE.search(f"{t.title}\n{t.body}")
    if m:
        return {"accepted": False, "triage_reason": f"out of scope for automation (matched '{m.group(0)}')"}
    gw = deps.gateway_for(state)
    raw, _ = gw.complete(Task(TaskKind.TRIAGE_CLASSIFY, prompts.triage(t), max_output_tokens=200,
                              node="triage"), run_id=state["run_id"])
    try:
        verdict = _json(raw)
        accepted, reason = bool(verdict.get("accept")), str(verdict.get("reason", ""))[:300]
    except (ValueError, json.JSONDecodeError):
        accepted, reason = False, "triage output unparseable; rejected by default"
    return {"accepted": accepted, "triage_reason": reason, "model_pins": gw.pins}


# ------------------------------------------------------------------ PLAN (Claude)
def plan(state, deps):
    repo = deps.repo_path(state)
    gw = deps.gateway_for(state)
    raw, _ = gw.complete(Task(TaskKind.PLAN, prompts.plan(state["ticket"], patches.repo_map(repo)),
                              max_output_tokens=8_192, node="plan"), run_id=state["run_id"])
    try:
        data = _json(raw)
        p = Plan(**{k: data[k] for k in ("summary", "target_files", "acceptance_criteria", "complexity") if k in data})
        for f in p.target_files:
            patches.validate_repo_path(f)
        t = state["ticket"]
        if t.criteria:                           # written by a person: used verbatim, whatever the model said
            p = p.model_copy(update={"acceptance_criteria": list(t.criteria)})
        if not p.acceptance_criteria:
            raise ValueError("no acceptance criteria")
    except Exception as e:                       # invalid plan: halt, a human looks at it
        return {"halt_reason": f"plan invalid: {e}"[:500], "model_pins": gw.pins}
    return {"plan": p, "model_pins": gw.pins, "criteria_source": "issue" if state["ticket"].criteria else "proposed"}


# ------------------------------------------------------------------ CRITERIA_GATE (a person, no model)
def criteria_gate(state, deps):
    """The graph stops before this node when the planner proposed the criteria (graph.compile_app). Only
    `runner.approve_criteria`, acting for a person, sets criteria_approved. Resuming any other way stops."""
    if not state.get("criteria_approved"):
        return {"halt_reason": "acceptance criteria were not approved by a person. Approve them with the "
                               "factory:criteria-ok label or make criteria-ok, not make resume"}
    if not state["plan"].acceptance_criteria:
        return {"halt_reason": "no acceptance criteria to test"}
    return {"criteria_source": "approved"}


# ------------------------------------------------------------------ WRITE_TESTS (Claude, independent)
def _check_tests(p: Plan, data: dict, repo: pathlib.Path) -> Plan:
    files, sources = list(data.get("test_files") or []), dict(data.get("test_sources") or {})
    if not files or set(files) != set(sources):
        raise PatchRejected("test_files and test_sources must name the same, non-empty set of files")
    for f in files:
        if not patches.validate_repo_path(f).startswith("tests/"):
            raise PatchRejected(f"test file outside tests/: {f}")
        if (repo / f).exists():
            raise PatchRejected(f"{f} already exists; tests must be new files")
    mapping = {str(k).strip(): v for k, v in (data.get("criteria_tests") or {}).items()}
    missing = [c for c in p.acceptance_criteria if not mapping.get(c.strip())]
    if missing:
        raise PatchRejected("no test for: " + "; ".join(missing)[:600])
    return p.model_copy(update={"test_files": files, "test_sources": sources,
                                "criteria_tests": {c: list(mapping[c.strip()]) for c in p.acceptance_criteria}})


def write_tests(state, deps):
    """A different model from the code author writes the tests, from the criteria and signatures only."""
    repo, p, t = deps.repo_path(state), state["plan"], state["ticket"]
    gw = deps.gateway_for(state)
    gw.preselect("generate_remote")              # know the code author now, so the test writer can differ
    feedback = state.get("tests_feedback")
    sigs = signatures.signatures(repo, p.target_files, t.language)
    style = signatures.test_style(repo, t.language)
    for attempt in (1, 2):                       # one immediate retry for a malformed or incomplete answer
        raw, d = gw.complete(Task(TaskKind.WRITE_TESTS, prompts.write_tests(t, p, sigs, style, feedback),
                                  max_output_tokens=8_192, node=f"write_tests#{attempt}"),
                             run_id=state["run_id"], independent_of=AUTHOR_ROLES)
        try:
            new_plan = _check_tests(p, _json(raw), repo)
            break
        except (PatchRejected, ValueError, json.JSONDecodeError) as e:
            feedback = str(e)
            if attempt == 2:
                return {"halt_reason": f"tests invalid: {e}"[:500], "model_pins": gw.pins}
    return {"plan": new_plan, "tests_feedback": None, "model_pins": gw.pins,
            "independence": {**(state.get("independence") or {}), **gw.independence}}


# ------------------------------------------------------------------ FREEZE_TESTS (T1)
def tree_sha256(root: pathlib.Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def freeze_tests(state, deps):
    run_dir, repo = deps.run_dir(state), deps.repo_path(state)
    frozen = run_dir / "frozen"
    if frozen.exists():
        for p in frozen.rglob("*"):
            p.chmod(stat.S_IRWXU)
        shutil.rmtree(frozen)
    if (repo / "tests").is_dir():
        shutil.copytree(repo / "tests", frozen, ignore=shutil.ignore_patterns("__pycache__"))
    frozen.mkdir(parents=True, exist_ok=True)
    for rel, src in state["plan"].test_sources.items():
        dest = frozen / rel.removeprefix("tests/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src)
    digest = tree_sha256(frozen)
    for p in [*frozen.rglob("*"), frozen]:          # read-only on disk as well as in the mount
        p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH | (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                                                              if p.is_dir() else 0))
    return {"tests_sha256": digest}


# ------------------------------------------------------------------ GENERATE (Claude by default)
def generate(state, deps):
    repo = deps.repo_path(state)
    p = state["plan"]
    gw = deps.gateway_for(state)
    raw, d = gw.complete(Task(TaskKind.GENERATE, prompts.generate(state, patches.read_files(repo, p.target_files)),
                              target_files=p.target_files, complexity=p.complexity,
                              max_output_tokens=8_192, node="generate"), run_id=state["run_id"])
    diff = patches.extract_and_validate(raw, repo)
    return {"patch": Patch(unified_diff=diff, produced_by=d.tier, model=d.model.model_id),
            "qa": None, "model_pins": gw.pins}


# ------------------------------------------------------------------ SANDBOX (T1, zero LLM calls)
def _parse_results(out: pathlib.Path) -> tuple[int, int]:
    junit, jest = out / "junit.xml", out / "jest.json"
    if junit.exists():
        root = ET.parse(junit).getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        total = sum(int(s.get("tests", 0)) for s in suites)
        bad = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
        skipped = sum(int(s.get("skipped", 0)) for s in suites)
        bad += sum(_proved_nothing(c) and c.find("failure") is None and c.find("error") is None
                   for c in root.iter("testcase"))
        return total - bad - skipped, bad
    if jest.exists():
        j = json.loads(jest.read_text() or "{}")
        return int(j.get("numPassedTests", 0)), int(j.get("numFailedTests", 0)) + int(j.get("numRuntimeErrorTestSuites", 0))
    return 0, 0


def _lint_errors(out: pathlib.Path) -> int:
    f = out / "lint.json"
    if not f.exists():
        return 0
    try:
        data = json.loads(f.read_text() or "[]")
    except json.JSONDecodeError:
        return 1
    if isinstance(data, list) and data and "code" in data[0]:        # ruff
        return len(data)
    return sum(int(x.get("errorCount", 0)) for x in data) if isinstance(data, list) else 0  # eslint


# A missing tool, dependency or test framework: the runner image is wrong, not the code. Halt, never repair.
ENVIRONMENT_RE = re.compile(
    r"ModuleNotFoundError: No module named|Cannot find module '(?!\.)|ERR_MODULE_NOT_FOUND|"
    r"could not determine executable to run|: command not found|factory: [\w-]+ not (installed|found)|"
    r"factory: (unsupported|no supported) |No tests found, exiting with code|"
    r"attempting to make an HTTP request, but --offline|no matching package named|failed to select a version|"
    r"failed to load source for dependency|Could not resolve dependencies for project|"
    r"Cannot access \S+ in offline mode|Plugin \S+ or one of its dependencies could not be resolved")
# The code (or a test) does not compile or parse: a syntax-class failure the model can repair.
SYNTAX_RE = re.compile(r"SyntaxError|IndentationError|Parsing error|TS\d{4}:|error\[E\d{4}\]|"
                       r"error: (expected|unexpected|mismatched)|COMPILATION ERROR|Parse Error:|"
                       r"Parser Error:|Compile Error:")
# Compile errors that only mean "the thing the test uses does not exist yet". A new test that fails this
# way does fail on the current code, which is what the red check wants to see.
MISSING_SYMBOL_RE = re.compile(
    r"error\[E0(425|432|433|412|422|599|061|560|609|616)\]|cannot find symbol|does not exist|"
    r"cannot be applied to|is not abstract and does not override|"
    r"not (found|declared) in (base|the current scope)|Identifier \"?\w+\"? not declared|"
    r"Could not find type|Function \"?\w+\"? not found|Could not preload resource|"
    r"Cannot get class|Invalid call\. Nonexistent function")


def classify_failure(out: pathlib.Path, broker: dict) -> tuple[FailureClass, int, int, int, str]:
    if broker.get("timed_out"):
        return FailureClass.TIMEOUT, 0, 0, 0, broker.get("log_tail", "")
    log = (out / "test.log").read_text(errors="replace") if (out / "test.log").exists() else broker.get("log_tail", "")
    if ENVIRONMENT_RE.search(log):                        # frozen tests the runner cannot find: a human must look
        return FailureClass.ENVIRONMENT, 0, 0, 0, log     # missing tool or dependency: rebuild the runner image
    try:
        result = json.loads((out / "result.json").read_text())
        test_exit, runner = result.get("test_exit"), result.get("runner")
    except (OSError, ValueError, AttributeError):
        test_exit = runner = None
    # pytest: 3 internal error, 4 usage error, 5 no tests collected -> the harness, not the model, is wrong.
    # Older runner images do not report `runner`; for them, "not Jest" meant pytest.
    is_pytest = runner == "python" or (runner is None and not (out / "jest.json").exists())
    if is_pytest and test_exit in (3, 4, 5):
        return FailureClass.ENVIRONMENT, 0, 0, 0, log
    lint = _lint_errors(out)
    passed, failed = _parse_results(out)
    if SYNTAX_RE.search(log):
        return FailureClass.SYNTAX, passed, failed, lint, log
    if failed or (passed == 0 and broker.get("exit_code", 0) != 0):
        return FailureClass.ASSERTION, passed, failed, lint, log
    if lint:
        lint_text = (out / "lint.json").read_text(errors="replace")[:6000]
        return FailureClass.SYNTAX, passed, failed, lint, lint_text
    return FailureClass.NONE, passed, failed, lint, ""


def write_results(dest: pathlib.Path, files: dict) -> pathlib.Path:
    """Write the broker's result files into a fresh, factory-private folder. The factory creates every
    file itself from JSON strings, so nothing the runner made (a symlink, a device) is ever opened here."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name, text in (files or {}).items():
        if name in RESULT_FILES and isinstance(text, str):
            (dest / name).write_text(text[:1_048_576])
    return dest


def _run_sandbox(state, deps, diff: str | None, results_dir: str) -> tuple[dict | None, pathlib.Path | None, str | None]:
    """Run the frozen tests against the base code plus `diff` (None = unchanged code) in the sealed
    runner. Returns (broker reply, results folder, halt reason)."""
    run_dir, repo = deps.run_dir(state), deps.repo_path(state)
    patches.reset_to_base(repo, state["base_sha"])
    if diff:
        patches.apply_patch(repo, diff)
    try:
        source = patches.source_archive(repo)
    except PatchRejected as e:                          # the repo itself, not the patch: a human must look
        return None, None, f"sandbox environment: {e}"
    req = {"run_id": state["run_id"], "language": state["ticket"].language,
           "source_tar_b64": base64.b64encode(source).decode(),
           "tests_tar_b64": base64.b64encode(patches.dir_archive(run_dir / "frozen")).decode(),
           "changed_files": patches.changed_files(diff) if diff else [],
           "image": state.get("runner_image")}
    resp = httpx.post(f"{deps.broker_url}/run", timeout=420, json=req)
    if resp.status_code >= 400:
        return None, None, f"sandbox environment: broker refused the run ({resp.status_code} {resp.text[:300]})"
    broker = resp.json()
    return broker, write_results(run_dir / results_dir, broker.get("files")), None


def new_test_outcome(results: pathlib.Path, new_files: list[str]) -> tuple[int, int]:
    """(tests found, tests failed) among the NEW test files only, from junit.xml (pytest) or jest.json.
    An error that stops a new file from loading (say, it imports a function that does not exist yet)
    counts as one failure: the tests do fail on the current code."""
    total = failed = 0
    junit, jest = results / "junit.xml", results / "jest.json"
    if junit.exists():
        for case in ET.parse(junit).getroot().iter("testcase"):
            if not _case_in(case, new_files) or case.find("skipped") is not None:
                continue
            total += 1
            failed += case.find("failure") is not None or case.find("error") is not None or _proved_nothing(case)
    elif jest.exists():
        for suite in json.loads(jest.read_text() or "{}").get("testResults", []):
            path = str(suite.get("name", ""))
            if not any(path.endswith("/" + f) or path == f for f in new_files):
                continue
            cases = suite.get("assertionResults") or []
            if not cases:                                # the file did not load at all
                total += 1
                failed += suite.get("status") == "failed"
                continue
            for c in cases:
                if c.get("status") in ("passed", "failed"):
                    total += 1
                    failed += c.get("status") == "failed"
    if total == 0:
        total, failed = _compile_failures(results, new_files)
    return total, failed


def _proved_nothing(case) -> bool:
    """GUT reports a test that never reached an assertion (say, it called a function that does not exist:
    a runtime error in GDScript, not a compile error) as passing with status "no asserts" or "risky".
    A test that proves nothing is not a pass."""
    return case.get("status") in ("no asserts", "risky")


def _case_in(case, new_files: list[str]) -> bool:
    """Does a JUnit testcase come from one of the new test files? Each runner names cases differently:
    pytest by module (tests.test_x) or file, Surefire by Java class (com.acme.GreeterTest), nextest by
    test binary (crate::test_x), GUT by script path (res://tests/test_x.gd)."""
    cls, name, file = case.get("classname", ""), case.get("name", ""), case.get("file", "")
    if file in new_files:
        return True
    for f in new_files:
        stem = pathlib.PurePosixPath(f).stem
        mod = f.removesuffix(".py").replace("/", ".")
        if f.endswith(".py") and (cls == mod or cls.startswith(mod + ".") or (not cls and name == mod)):
            return True
        if f.endswith(".java") and (cls == stem or cls.endswith("." + stem)):
            return True
        if f.endswith(".rs") and (cls == stem or cls.endswith("::" + stem)):
            return True
        if f.endswith(".gd") and (cls.endswith("/" + f) or cls == f or cls == "res://" + f):
            return True
    return False


def _compile_failures(results: pathlib.Path, new_files: list[str]) -> tuple[int, int]:
    """Compiled languages build every test together, so a new test that calls a function that does not
    exist yet stops the build and no report is written. Count each new file that a missing-symbol error
    names as one failed test. Any other compile error counts as nothing ran, which sends the tests back."""
    log_file = results / "test.log"
    log = log_file.read_text(errors="replace") if log_file.exists() else ""
    if not MISSING_SYMBOL_RE.search(log):
        return 0, 0
    named = [f for f in new_files if pathlib.PurePosixPath(f).name in log]
    return len(named), len(named)


def red_check(state, deps):
    """No model: run the new tests against the UNCHANGED code. Tests that already pass do not test the
    change, so at least one must fail. Otherwise they go back to the test writer once, then the run stops."""
    p = state["plan"]
    broker, results, halt = _run_sandbox(state, deps, None, "red")
    if halt:
        return {"halt_reason": halt}
    total, failed = new_test_outcome(results, p.test_files)
    rc = RedCheck(new_tests=total, new_failed=failed,
                  detail=f"{failed} of {total} new tests fail on the current code")
    if failed >= 1:
        return {"red_check": rc}
    why = ("none of the new tests ran: check the file names (tests/test_*.py or tests/*.test.js) and imports"
           if total == 0 else f"all {total} new tests already pass on the current code, so they do not test the change")
    if state.get("test_revisions", 0) >= 1:
        return {"red_check": rc, "halt_reason": f"tests do not fail on the current code: {why}"}
    return {"red_check": rc, "tests_feedback": why, "test_revisions": state.get("test_revisions", 0) + 1}


def sandbox(state, deps):
    run_dir = deps.run_dir(state)
    broker, results, halt = _run_sandbox(state, deps, state["patch"].unified_diff, "results")
    if halt:
        return {"halt_reason": halt}
    hash_ok = tree_sha256(run_dir / "frozen") == state["tests_sha256"]
    fc, passed, failed, lint, excerpt = classify_failure(results, broker)
    if not hash_ok:
        fc = FailureClass.TAMPER
    return {"sandbox": SandboxResult(exit_code=int(broker.get("exit_code", 1)), passed=passed, failed=failed,
                                     lint_errors=lint, failure_class=fc,
                                     stack_excerpt=redact(excerpt)[-8000:],
                                     duration_s=float(broker.get("duration_s", 0.0)), tests_hash_ok=hash_ok)}


# ------------------------------------------------------------------ QA (T1)
def qa(state, deps):
    f = deps.run_dir(state) / "results" / "qa.json"
    high = medium = 0
    report = ""
    if f.exists():
        try:
            data = json.loads(f.read_text() or "{}")
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict) and "results" in data:                    # bandit
            for r in data["results"]:
                sev = r.get("issue_severity", "").upper()
                high += sev == "HIGH"
                medium += sev == "MEDIUM"
            report = json.dumps([{k: r.get(k) for k in ("filename", "line_number", "issue_severity", "issue_text")}
                                 for r in data["results"]][:40])
        elif isinstance(data, list):                                        # eslint-plugin-security
            msgs = [dict(m, file=x.get("filePath")) for x in data for m in x.get("messages", [])]
            high = sum(m.get("severity") == 2 for m in msgs)
            medium = sum(m.get("severity") == 1 for m in msgs)
            report = json.dumps([{k: m.get(k) for k in ("file", "line", "ruleId", "message")} for m in msgs][:40])
    return {"qa": QAResult(high_findings=high, medium_findings=medium, report_excerpt=report[:4000])}


# ------------------------------------------------------------------ REVIEW_CODE (Claude, independent)
def review_code(state, deps):
    """A model other than the code author reviews the diff. It reports findings; the factory decides which
    block (pool.yaml `review`). It can send the change back to repair, never approve it."""
    if state.get("qualification"):
        return {"review": None}                 # reviewers are measured by their own suite
    p, t = state["plan"], state["ticket"]
    gw = deps.gateway_for(state)
    sb = state.get("sandbox")
    raw, d = gw.complete(Task(TaskKind.REVIEW_CODE,
                              prompts.review_code(t, p, state["patch"].unified_diff, sb.passed if sb else 0, p.test_files),
                              max_output_tokens=4_096, node=f"review_code#{state.get('repair_attempts', 0)}"),
                         run_id=state["run_id"], independent_of=AUTHOR_ROLES)
    independent = gw.independence.get("review_code", "independent") == "independent"
    rv = review.parse(raw, model=d.model.model_id, independent=independent)
    return {"review": rv, "model_pins": gw.pins,
            "independence": {**(state.get("independence") or {}), **gw.independence}}


# ------------------------------------------------------------------ REPAIR (Claude, or one local syntax retry)
TRACE_PATH = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|rs|java|gd))")


def mentioned_files(repo: pathlib.Path, log: str) -> list[str]:
    """Repo-relative source files named in a traceback or lint report. Jest, ESLint and pytest often print
    absolute paths (/workspace/src/x.js); those are mapped back, and anything outside the repo is dropped."""
    out = set()
    for m in TRACE_PATH.findall(log or ""):
        for prefix in (f"{repo}/", "/workspace/", "//"):     # "//": Godot's res://, after the scheme
            if m.startswith(prefix):
                m = m[len(prefix):]
        m = m.removeprefix("./")
        if m.startswith(("/", "node_modules/", ".factory/", ".git/", "target/", "addons/gut/")):
            continue
        try:
            patches.validate_repo_path(m)
        except PatchRejected:
            continue
        if (repo / m).is_file():
            out.add(m)
    return sorted(out)


def repair(state, deps):
    repo = deps.repo_path(state)
    tier, role = prompts.next_repair_role(state)
    sb = state.get("sandbox")
    mentioned = mentioned_files(repo, sb.stack_excerpt if sb else "")
    files = list(dict.fromkeys([*state["plan"].target_files, *mentioned]))[:12]
    patches.reset_to_base(repo, state["base_sha"])          # sources shown to the model = original base
    kind = TaskKind.SECURITY_FIX if role == "security_fix" else TaskKind.REPAIR
    gw = deps.gateway_for(state)
    n = state.get("repair_attempts", 0) + 1
    raw, d = gw.complete(Task(kind, prompts.repair(state, patches.read_files(repo, files), state.get("attempts", [])),
                              target_files=state["plan"].target_files, complexity=state["plan"].complexity,
                              escalation_required=(tier is Tier.T3_REMOTE and kind is TaskKind.REPAIR),
                              max_output_tokens=8_192, node=f"repair#{n}"), run_id=state["run_id"])
    diff = patches.extract_and_validate(raw, repo)
    record = {"attempt": n, "tier": d.tier.value, "model": d.model.model_id,
              "failure_class": sb.failure_class.value if sb else "none",
              "diff_stat": patches.git(repo, "apply", "--stat", "-", input=diff.encode()).stdout.decode()[:1500]}
    return {"patch": Patch(unified_diff=diff, produced_by=d.tier, model=d.model.model_id),
            "repair_attempts": n, "qa": None, "review": None, "model_pins": gw.pins, "attempts": [record]}


# ------------------------------------------------------------------ COMPILE_PR (T1, human-gated)
def compile_pr(state, deps):
    if state.get("dry_run"):
        return {"pr_url": "dry-run"}
    t, repo = state["ticket"], deps.repo_path(state)
    branch = "factory/" + re.sub(r"[^a-z0-9-]+", "-", t.ticket_id.lower())
    gitenv = {**deps.git_auth_env(),
              "GIT_AUTHOR_NAME": "AI Software Factory", "GIT_AUTHOR_EMAIL": "factory@localhost",
              "GIT_COMMITTER_NAME": "AI Software Factory", "GIT_COMMITTER_EMAIL": "factory@localhost"}
    patches.reset_to_base(repo, state["base_sha"])
    patches.apply_patch(repo, state["patch"].unified_diff)
    for f in state["plan"].test_sources:                    # ship the acceptance tests with the change
        dest = repo / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(state["plan"].test_sources[f])
    for args in (["checkout", "-B", branch], ["add", "-A"],
                 ["commit", "--no-verify", "-m", f"{t.ticket_id}: {t.title}"[:200]],
                 ["push", "--no-verify", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}"]):
        r = patches.git(repo, *args, check=False, env=gitenv)
        if r.returncode != 0:
            return {"halt_reason": f"git {args[0]} failed: " + r.stderr.decode(errors="replace")[-400:]}
    spent = deps.breaker_for(state["run_id"]).spent()
    closes = f"Closes #{t.issue_number}\n\n" if t.issue_number else ""
    body = (f"{closes}Automated change for **{t.ticket_id}**.\n\n{state['plan'].summary}\n\n"
            "**Acceptance criteria** (" + {"issue": "written in the issue",
                                            "approved": "proposed by the planner, approved by a person"}.get(
                state.get("criteria_source") or "", "source unknown") + ")\n"
            + "\n".join(f"- {c}" for c in state["plan"].acceptance_criteria)
            + ("\n\n**Check by hand** (no test can judge these)\n" + "\n".join(f"- [ ] {c}" for c in t.manual_checks)
               if t.manual_checks else "") +
            f"\n\n**Run**: `{state['run_id']}` · repairs: {state.get('repair_attempts', 0)} · "
            f"billed cost: ${spent:.4f} · models: {json.dumps(state.get('model_pins', {}))}\n\n"
            f"**Checks**: {state['red_check'].detail if state.get('red_check') else 'tests-fail-first check not run'} "
            f"before the change; tests written by "
            f"{(state.get('model_pins') or {}).get('write_tests', '?')} "
            f"({(state.get('independence') or {}).get('write_tests', 'independence unknown')}).\n\n"
            f"{review.summary(state.get('review'))}\n\n"
            + (f"**Trace**: {trace_url(state['run_id'])}\n" if trace_url(state["run_id"]) else ""))
    r = httpx.post(f"https://api.github.com/repos/{t.repo}/pulls", timeout=30,
                   headers={"Authorization": f"Bearer {deps.github_token()}",
                            "Accept": "application/vnd.github+json"},
                   json={"title": f"{t.ticket_id}: {t.title}"[:250], "head": branch, "base": t.base_branch,
                         "body": redact(body), "draft": True})
    if r.status_code == 422 and "already exists" in r.text:
        return {"pr_url": f"https://github.com/{t.repo}/pulls?q=head:{branch}"}
    if r.status_code >= 300:
        return {"halt_reason": f"PR creation failed: HTTP {r.status_code} {r.text[:300]}"}
    return {"pr_url": r.json()["html_url"]}


# ------------------------------------------------------------------ HALT (never guarded)
def halt(state):
    reason = state.get("halt_reason")
    if not reason:
        sb, qa_ = state.get("sandbox"), state.get("qa")
        if state.get("accepted") is False:
            reason = f"triage rejected: {state.get('triage_reason', '')}"
        elif sb is not None and sb.failure_class in (FailureClass.TAMPER, FailureClass.ENVIRONMENT):
            reason = f"sandbox {sb.failure_class.value}: needs a human"
        elif qa_ is not None and qa_.high_findings:
            reason = f"repair limit reached with {qa_.high_findings} high security findings"
        elif state.get("review") is not None and state["review"].blocked:
            cats = sorted({f.category for f in state["review"].findings if f.blocking})
            reason = f"repair limit reached: the independent review still blocks ({', '.join(cats)})"
        else:
            reason = f"repair limit reached ({state.get('repair_attempts', 0)} cycles)"
    if not reason.startswith(("breaker:", "triage rejected")):
        alert(state["run_id"], state["ticket"].ticket_id, reason)
    else:
        print(f"[halt] {state['ticket'].ticket_id}: {reason}", flush=True)
    return {"halt_reason": reason}
