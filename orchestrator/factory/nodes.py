"""FSM nodes (spec §1.3). Each takes (state, deps) and returns a partial state update."""
from __future__ import annotations

import hashlib
import html
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import xml.etree.ElementTree as ET

import httpx

from factory import patches, prompts
from factory.breaker import PatchRejected, alert
from factory.gateway import Task, TaskKind
from factory.obs import trace_url
from factory.state import (FailureClass, Patch, Plan, QAResult, SandboxResult, Tier, TicketSpec)

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]{1,100}$")
REJECT_RE = re.compile(r"(?i)\b(migrations?|terraform|kubernetes|helm|secrets?|credentials?|passwords?|"
                       r"api[ _-]?keys?|\.github/workflows|ci pipeline|infrastructure|dockerfile)\b")
SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+")
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
    ticket = t.model_copy(update={"body": body, "title": t.title[:300]})
    repo = deps.repo_path(state)
    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "--depth", "50", "--branch", t.base_branch,
                            f"https://github.com/{t.repo}.git", str(repo)],
                           capture_output=True, env={**os.environ, **deps.git_auth_env()})
        if r.returncode != 0:
            return {"halt_reason": "clone failed: " + r.stderr.decode(errors="replace")[-500:]}
    sha = patches.git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    return {"ticket": ticket, "base_sha": sha}


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
        p = Plan(**_json(raw))
        for f in p.target_files:
            patches.validate_repo_path(f)
        for f in p.test_files:
            if not patches.validate_repo_path(f).startswith("tests/"):
                raise PatchRejected(f"test file outside tests/: {f}")
        if set(p.test_files) != set(p.test_sources):
            raise PatchRejected("test_files and test_sources keys differ")
    except Exception as e:                       # invalid plan: halt, a human looks at it
        return {"halt_reason": f"plan invalid: {e}"[:500], "model_pins": gw.pins}
    return {"plan": p, "model_pins": gw.pins}


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


def classify_failure(out: pathlib.Path, broker: dict) -> tuple[FailureClass, int, int, int, str]:
    if broker.get("timed_out"):
        return FailureClass.TIMEOUT, 0, 0, 0, broker.get("log_tail", "")
    log = (out / "test.log").read_text(errors="replace") if (out / "test.log").exists() else broker.get("log_tail", "")
    if re.search(r"ModuleNotFoundError: No module named|Cannot find module '(?!\.)|ERR_MODULE_NOT_FOUND", log):
        return FailureClass.ENVIRONMENT, 0, 0, 0, log
    try:
        test_exit = json.loads((out / "result.json").read_text()).get("test_exit")
    except (OSError, ValueError):
        test_exit = None
    # pytest: 3 internal error, 4 usage error, 5 no tests collected -> the harness, not the model, is wrong
    if test_exit in (3, 4, 5) and not (out / "jest.json").exists():
        return FailureClass.ENVIRONMENT, 0, 0, 0, log
    lint = _lint_errors(out)
    passed, failed = _parse_results(out)
    if re.search(r"SyntaxError|IndentationError|Parsing error|TS\d{4}:", log):
        return FailureClass.SYNTAX, passed, failed, lint, log
    if failed or (passed == 0 and broker.get("exit_code", 0) != 0):
        return FailureClass.ASSERTION, passed, failed, lint, log
    if lint:
        lint_text = (out / "lint.json").read_text(errors="replace")[:6000]
        return FailureClass.SYNTAX, passed, failed, lint, lint_text
    return FailureClass.NONE, passed, failed, lint, ""


def sandbox(state, deps):
    run_dir, repo = deps.run_dir(state), deps.repo_path(state)
    patches.reset_to_base(repo, state["base_sha"])
    patches.apply_patch(repo, state["patch"].unified_diff)
    (repo / ".factory").mkdir(exist_ok=True)
    (repo / ".factory" / "changed_files.txt").write_text("\n".join(patches.changed_files(state["patch"].unified_diff)))
    broker = httpx.post(f"{deps.broker_url}/run", timeout=360,
                        json={"run_id": state["run_id"], "language": state["ticket"].language}).json()
    hash_ok = tree_sha256(run_dir / "frozen") == state["tests_sha256"]
    fc, passed, failed, lint, excerpt = classify_failure(repo / ".factory", broker)
    if not hash_ok:
        fc = FailureClass.TAMPER
    return {"sandbox": SandboxResult(exit_code=int(broker.get("exit_code", 1)), passed=passed, failed=failed,
                                     lint_errors=lint, failure_class=fc,
                                     stack_excerpt=SECRET_RE.sub(r"\1=<redacted>", excerpt)[-8000:],
                                     duration_s=float(broker.get("duration_s", 0.0)), tests_hash_ok=hash_ok)}


# ------------------------------------------------------------------ QA (T1)
def qa(state, deps):
    f = deps.repo_path(state) / ".factory" / "qa.json"
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


# ------------------------------------------------------------------ REPAIR (Claude, or one local syntax retry)
TRACE_PATH = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|ts|tsx|js|jsx|mjs))")


def repair(state, deps):
    repo = deps.repo_path(state)
    tier, role = prompts.next_repair_role(state)
    sb = state.get("sandbox")
    mentioned = {m for m in TRACE_PATH.findall(sb.stack_excerpt if sb else "") if (repo / m).is_file()}
    files = list(dict.fromkeys([*state["plan"].target_files, *sorted(mentioned)]))[:12]
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
            "repair_attempts": n, "qa": None, "model_pins": gw.pins, "attempts": [record]}


# ------------------------------------------------------------------ COMPILE_PR (T1, human-gated)
def compile_pr(state, deps):
    if state.get("dry_run"):
        return {"pr_url": "dry-run"}
    t, repo = state["ticket"], deps.repo_path(state)
    branch = "factory/" + re.sub(r"[^a-z0-9-]+", "-", t.ticket_id.lower())
    gitenv = {**os.environ, **deps.git_auth_env(),
              "GIT_AUTHOR_NAME": "AI Software Factory", "GIT_AUTHOR_EMAIL": "factory@localhost",
              "GIT_COMMITTER_NAME": "AI Software Factory", "GIT_COMMITTER_EMAIL": "factory@localhost"}
    patches.reset_to_base(repo, state["base_sha"])
    patches.apply_patch(repo, state["patch"].unified_diff)
    for f in state["plan"].test_sources:                    # ship the acceptance tests with the change
        dest = repo / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(state["plan"].test_sources[f])
    for args in (["checkout", "-B", branch], ["add", "-A"],
                 ["commit", "-m", f"{t.ticket_id}: {t.title}"[:200]],
                 ["push", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}"]):
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=gitenv)
        if r.returncode != 0:
            return {"halt_reason": f"git {args[0]} failed: " + r.stderr.decode(errors="replace")[-400:]}
    spent = deps.breaker_for(state["run_id"]).spent()
    closes = f"Closes #{t.issue_number}\n\n" if t.issue_number else ""
    body = (f"{closes}Automated change for **{t.ticket_id}**.\n\n{state['plan'].summary}\n\n"
            "**Acceptance criteria**\n" + "\n".join(f"- {c}" for c in state["plan"].acceptance_criteria) +
            f"\n\n**Run**: `{state['run_id']}` · repairs: {state.get('repair_attempts', 0)} · "
            f"billed cost: ${spent:.4f} · models: {json.dumps(state.get('model_pins', {}))}\n"
            f"**Trace**: {trace_url(state['run_id'])}\n")
    r = httpx.post(f"https://api.github.com/repos/{t.repo}/pulls", timeout=30,
                   headers={"Authorization": f"Bearer {deps.github_token()}",
                            "Accept": "application/vnd.github+json"},
                   json={"title": f"{t.ticket_id}: {t.title}"[:250], "head": branch, "base": t.base_branch,
                         "body": body, "draft": True})
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
        else:
            reason = f"repair limit reached ({state.get('repair_attempts', 0)} cycles)"
    if not reason.startswith(("breaker:", "triage rejected")):
        alert(state["run_id"], state["ticket"].ticket_id, reason)
    else:
        print(f"[halt] {state['ticket'].ticket_id}: {reason}", flush=True)
    return {"halt_reason": reason}
