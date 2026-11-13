"""Gate 3: a deterministic check routes architecture-changing patches to a person."""
from __future__ import annotations

import subprocess

import pytest
from langgraph.checkpoint.memory import MemorySaver

from factory import archgate, graph, nodes, runner
from factory.state import FailureClass, Patch, Tier
from test_graph_flow import ARCH, StubDeps, TICKET, stub_nodes  # noqa: F401  (fixture)

REAL_COMPILE_PR = nodes.compile_pr                                  # before any fixture stubs it


def diff_of(*paths, added=()):
    out = []
    for p in paths:
        out += [f"--- a/{p}", f"+++ b/{p}", "@@ -1 +1 @@", "-a", "+b"]
    for p in added:
        out += ["--- /dev/null", f"+++ b/{p}", "@@ -0,0 +1 @@", "+b"]
    return "\n".join(out) + "\n"


@pytest.mark.parametrize("path,why", [
    ("Dockerfile", "container"), ("docker/Dockerfile.dev", "container"), ("compose.yaml", "container"),
    ("infra/main.tf", "infrastructure"), ("deploy/values.yaml", "infrastructure"), ("charts/app/x.yaml", "infrastructure"),
    ("build.rs", "build or install"), ("Makefile", "build or install"), ("scripts/release.sh", "build or install"),
    (".cargo/config.toml", "build or install"), (".mvn/wrapper/x.properties", "build or install"),
    ("conftest.py", "test harness"), ("jest.config.js", "test harness"), (".gutconfig.json", "test harness"),
    (".env.production", "environment"), (".npmrc", "environment"),
    ("CODEOWNERS", "ownership"), (".gitmodules", "ownership"), ("App.entitlements", "ownership"),
    ("project.godot", "engine"), ("export_presets.cfg", "engine"), ("addons/dialogue/plugin.cfg", "engine"),
])
def test_architecture_changes_are_flagged_with_a_reason(path, why):
    (reason,) = archgate.triggers(diff_of(path), {"docker", "infra", "deploy", "charts", "scripts", "addons", ".mvn",
                                                  ".cargo"})
    assert reason.startswith(path) and why in reason


@pytest.mark.parametrize("path", ["src/greet.js", "src/lib.rs", "src/main/java/com/x/A.java", "scripts/player.gd",
                                  "scenes/main.tscn", "README.md", "docs/guide.md", "assets/logo.png"])
def test_ordinary_code_changes_pass_untouched(path):
    assert archgate.triggers(diff_of(path), {"src", "scripts", "scenes", "docs", "assets"}) == []


def test_a_new_top_level_folder_is_a_new_module():
    d = diff_of(added=["payments/api.py", "payments/db.py", "src/new_helper.py", "NOTES.md"])
    assert archgate.triggers(d, {"src"}) == ["payments/: new top-level folder (a new module, package or service)"]


def test_renames_are_judged_by_both_names():
    d = "--- a/scripts/run.txt\n+++ b/scripts/run.sh\n@@ -1 +1 @@\n-a\n+b\n"
    assert archgate.triggers(d, {"scripts"}) == ["scripts/run.sh: code that runs at build or install time"]


def test_arch_check_reads_the_base_tree_not_the_patched_one(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
                   check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "billing").mkdir()                                     # present on disk, not in the base commit

    class D:
        def repo_path(self, s):
            return repo
    s = {"base_sha": sha, "patch": Patch(unified_diff=diff_of("Dockerfile", added=["billing/x.py", "src/b.py"]),
                                         produced_by=Tier.T3_REMOTE, model="m")}
    assert nodes.arch_check(s, D())["arch_triggers"] == [
        "Dockerfile: container image or compose file",
        "billing/: new top-level folder (a new module, package or service)"]


# ------------------------------------------------------------------ the graph and the runner
def _green(stub_nodes, triggers):
    _, outcomes = stub_nodes
    outcomes.append(FailureClass.NONE)
    ARCH.clear()
    ARCH.append(triggers)


def _start(app, run_id):
    cfg = graph.config_for(run_id)
    app.invoke({"run_id": run_id, "ticket": TICKET, "repair_attempts": 0, "model_pins": {}, "attempts": []}, cfg)
    return cfg


def test_triggered_change_waits_for_an_architecture_approval_then_pushes(stub_nodes, monkeypatch):
    _green(stub_nodes, ["Dockerfile: container image or compose file"])
    monkeypatch.setattr(nodes, "compile_pr", lambda s, d: {"pr_url": "https://example/pr/9"}
                        if s.get("arch_approved") else {"halt_reason": "not approved"})
    app = graph.compile_app(StubDeps(), MemorySaver())               # nodes are bound when the graph is built
    cfg = _start(app, "run-arch-0001")
    assert app.get_state(cfg).next == ("compile_pr",)
    assert runner.outcome(app, cfg) == ("awaiting_arch_approval", "")
    note = runner.approval_note(app.get_state(cfg).values)
    assert "Dockerfile: container image" in note and "threat note" in note
    monkeypatch.setattr(runner, "compile_app", lambda deps, s: app)
    runner.approve_arch(StubDeps(), None, "run-arch-0001")
    assert app.invoke(None, cfg)["pr_url"] == "https://example/pr/9"


def test_resuming_a_triggered_change_without_approval_halts(stub_nodes, monkeypatch):
    _green(stub_nodes, ["build.rs: code that runs at build or install time"])
    monkeypatch.setattr(nodes, "compile_pr", REAL_COMPILE_PR)       # the real node, not the stub
    app = graph.compile_app(StubDeps(), MemorySaver())
    cfg = _start(app, "run-arch-0002")
    final = app.invoke(None, cfg)                                   # make resume / make approve without the gate
    assert "alters the architecture" in final["halt_reason"]


def test_untriggered_changes_keep_the_ordinary_approval(stub_nodes, monkeypatch):
    _green(stub_nodes, [])
    app = graph.compile_app(StubDeps(), MemorySaver())
    cfg = _start(app, "run-arch-0003")
    assert runner.outcome(app, cfg) == ("awaiting_approval", "")
    monkeypatch.setattr(runner, "compile_app", lambda deps, s: app)
    with pytest.raises(SystemExit, match="not waiting for an architecture approval"):
        runner.approve_arch(StubDeps(), None, "run-arch-0003")


def test_route_after_a_clean_review_always_passes_the_architecture_check():
    from factory.state import Review
    assert graph.route_after_review({"review": None}) == "arch_check"
    assert graph.route_after_review({"review": Review(model="m", independent=True)}) == "arch_check"


def test_board_shows_why_and_both_ways_to_approve():
    from factory.board import NODE_STATION, card_action
    assert NODE_STATION["arch_check"] == "qa"
    detail = runner.arch_note({"arch_triggers": ["Dockerfile: container image or compose file"]})
    a = card_action({"status": "awaiting_arch_approval", "ref": "o/r#5", "id": "run-y", "number": 5,
                     "issue_url": "https://github.com/o/r/issues/5", "detail": detail})
    text = " ".join(s.get("text", "") + s.get("command", "") for s in a["steps"])
    assert "Dockerfile: container image" in a["why"] and "factory:arch-approve" in text
    assert "make arch-ok RUN=run-y" in text and "make diff RUN=run-y" in text
