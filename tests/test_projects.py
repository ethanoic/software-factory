"""Projects: the registry, fair scheduling, the checks at the start of a run, and per-project images.
Pure tests need nothing; the rest need FACTORY_TEST_DSN (see test_postgres.py)."""
import io
import os
import pathlib
import subprocess
import sys
import tarfile
import time

import psycopg
import pytest
import yaml

from factory import depshash, projects
from factory.projects import ProjectError
from factory.runner import pick_next
from factory.state import TicketSpec

ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get("FACTORY_TEST_DSN")
needs_db = pytest.mark.skipif(not DSN, reason="set FACTORY_TEST_DSN")


# ------------------------------------------------------------------ fair scheduling (no database)
def T(name):
    return pathlib.Path(f"/inbox/{name}.json")


def test_fewest_runs_today_goes_first_then_oldest_ticket():
    waiting = [(T("a1"), "o/a", 1.0), (T("a2"), "o/a", 2.0), (T("b1"), "o/b", 3.0)]
    assert pick_next(waiting, {"o/a": 2, "o/b": 0}, {}) == T("b1")          # o/b has had no runs today
    assert pick_next(waiting, {"o/a": 1, "o/b": 1}, {}) == T("a1")          # tie: oldest ticket wins
    assert pick_next(waiting, {}, {}) == T("a1")


def test_a_project_at_its_cap_waits_and_others_continue():
    waiting = [(T("a1"), "o/a", 1.0), (T("b1"), "o/b", 5.0)]
    assert pick_next(waiting, {"o/a": 2}, {"o/a": 2}) == T("b1")
    assert pick_next([(T("a1"), "o/a", 1.0)], {"o/a": 2}, {"o/a": 2}) is None
    assert pick_next([(T("a1"), "o/a", 1.0)], {"o/a": 0}, {"o/a": 0}) is None   # cap 0 = no runs
    assert pick_next([], {}, {}) is None


def test_busy_project_cannot_starve_a_quiet_one():
    """Ten tickets for o/a and one for o/b: o/b runs second, not eleventh."""
    waiting = [(T(f"a{i}"), "o/a", float(i)) for i in range(10)] + [(T("b"), "o/b", 99.0)]
    used, order = {}, []
    for _ in range(3):
        f = pick_next(waiting, used, {})
        order.append(f.stem)
        repo = next(r for p, r, _ in waiting if p == f)
        used[repo] = used.get(repo, 0) + 1
        waiting = [w for w in waiting if w[0] != f]
    assert order == ["a0", "b", "a1"]


# ------------------------------------------------------------------ dependency hash (no database)
def test_deps_hash_follows_the_files_the_image_installs(tmp_path):
    (tmp_path / "package.json").write_text('{"devDependencies": {"jest": "29"}}')
    (tmp_path / "package-lock.json").write_text("{}")
    (tmp_path / "README.md").write_text("x")
    h = depshash.deps_hash(tmp_path, "javascript")
    assert depshash.deps_files(tmp_path, "javascript") == ["package-lock.json", "package.json"]
    (tmp_path / "README.md").write_text("changed")                          # not a dependency file
    assert depshash.deps_hash(tmp_path, "javascript") == h
    (tmp_path / "package-lock.json").write_text('{"v": 2}')
    assert depshash.deps_hash(tmp_path, "javascript") != h
    assert depshash.deps_hash(tmp_path, "python") != h                       # language is part of the hash
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    (tmp_path / "requirements.txt").symlink_to("/etc/passwd")                # links are ignored
    assert depshash.deps_files(tmp_path, "python") == ["requirements-dev.txt"]


def test_image_names_are_tied_to_project_and_hash():
    h = "a" * 64
    assert projects.image_name("Ethanoic/Factory-Hello-Node", h) == "factory-runner-ethanoic-factory-hello-node:aaaaaaaaaaaa"
    assert projects.IMAGE_RE.fullmatch(projects.image_name("o/r", h))


def test_broker_accepts_only_factory_runner_images():
    pytest.importorskip("fastapi")
    pytest.importorskip("docker")
    sys.path.insert(0, str(ROOT / "sandbox_broker"))
    sys.modules.pop("app", None)
    import app as broker
    ok = broker.PROJECT_IMAGE.fullmatch
    assert ok("factory-runner-o-r:0123456789ab")
    for bad in ("alpine:latest", "factory-runner-o-r:latest", "evil/factory-runner-o-r:0123456789ab",
                "factory-runner-o-r:0123456789ab --privileged", "factory-runner-O-R:0123456789ab"):
        assert not ok(bad), bad


# ------------------------------------------------------------------ the stack is self-contained
def test_compose_brings_its_own_database_and_needs_no_other_compose_file():
    c = yaml.safe_load((ROOT / "compose.factory.yml").read_text())
    pg = c["services"]["postgres"]
    assert pg["image"].startswith("postgres:16.") and pg["networks"] == ["data"] and "ports" not in pg
    assert "factory-db:/var/lib/postgresql/data" in pg["volumes"] and "factory-db" in c["volumes"]
    for svc in ("orchestrator", "pool-worker", "board"):
        assert c["services"][svc]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "langfuse-web" not in c["services"]
    make = (ROOT / "Makefile").read_text()
    assert "-f compose.yml" not in make and "-f compose.factory.yml" in make


# ------------------------------------------------------------------ registry and run checks (Postgres)
@pytest.fixture()
def db():
    if not DSN:
        pytest.skip("set FACTORY_TEST_DSN")
    from factory.runner import migrate
    migrate(DSN)
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("TRUNCATE projects, runs")
    return DSN


@needs_db
def test_registry_add_pause_limit_labelers_image_remove(db):
    p = projects.add(db, "Owner/Repo", "javascript", "develop", max_runs_per_day=2)
    assert (p.repo, p.base_branch, p.max_runs_per_day, p.ready) == ("owner/repo", "develop", 2, False)
    with pytest.raises(ProjectError, match="already"):
        projects.add(db, "owner/repo", "python")
    for bad in (("ethanoic/software-factory", "python"), ("no-slash", "python"), ("o/r2", "go")):
        with pytest.raises(ProjectError):
            projects.add(db, *bad)
    h = "b" * 64
    with pytest.raises(ProjectError, match="does not match"):
        projects.set_image(db, "owner/repo", "alpine:latest", h)
    assert projects.set_image(db, "owner/repo", projects.image_name("owner/repo", h), h).ready
    assert projects.set_status(db, "owner/repo", "paused").ready is False
    assert [p.repo for p in projects.ready(db)] == []
    projects.set_status(db, "owner/repo", "active")
    assert projects.set_limit(db, "owner/repo", None).max_runs_per_day is None
    assert projects.set_labelers(db, "owner/repo", ["alice", "bob-2"]).labelers == ["alice", "bob-2"]
    with pytest.raises(ProjectError):
        projects.set_labelers(db, "owner/repo", ["bad login!"])
    assert projects.ready(db)[0].as_intake()["labelers"] == ["alice", "bob-2"]
    projects.remove(db, "owner/repo")
    assert projects.get(db, "owner/repo") is None


@needs_db
def test_github_yaml_repos_move_into_projects_once(db):
    cfg = {"repos": [{"repo": "o/a", "language": "python"}, {"repo": "o/b", "language": "typescript",
                                                            "base_branch": "dev"}]}
    assert projects.import_from_yaml(db, cfg) == ["o/a", "o/b"]
    assert projects.import_from_yaml(db, cfg) == []
    assert projects.get(db, "o/b").base_branch == "dev"


class _Deps:
    def __init__(self, dsn, root):
        self.dsn, self.root = dsn, root

    def repo_path(self, state):
        return self.root / "repo"

    def git_auth_env(self):
        return {}


def _origin(tmp_path, files: dict) -> pathlib.Path:
    """A local repo standing in for GitHub: ingest's clone URL is rewritten to it with git's insteadOf."""
    src = tmp_path / "origin" / "o" / "r.git"
    work = tmp_path / "work"
    work.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(work), *a], check=True, capture_output=True)  # noqa: E731
    g("init", "-q", "-b", "main")
    for n, text in files.items():
        (work / n).write_text(text)
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    src.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(src)], check=True)
    return work


def _ingest(tmp_path, db, ticket, monkeypatch, qualification=False):
    from factory.nodes import ingest
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{tmp_path}/origin/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:https")
    deps = _Deps(db, tmp_path / f"run{time.time_ns()}")
    return ingest({"ticket": ticket, "run_id": "r", "qualification": qualification}, deps)


@needs_db
def test_ingest_refuses_unregistered_paused_and_unbuilt_projects(db, tmp_path, monkeypatch):
    t = TicketSpec(ticket_id="o/r#1", title="t", body="b", repo="o/r", language="python", issue_number=1)
    assert "not a registered project" in _ingest(tmp_path, db, t, monkeypatch)["halt_reason"]
    projects.add(db, "o/r", "javascript")
    assert "no runner image" in _ingest(tmp_path, db, t, monkeypatch)["halt_reason"]
    projects.set_status(db, "o/r", "paused")
    assert "paused" in _ingest(tmp_path, db, t, monkeypatch)["halt_reason"]


@needs_db
def test_ingest_uses_the_project_settings_and_catches_dependency_drift(db, tmp_path, monkeypatch):
    work = _origin(tmp_path, {"package.json": '{"name": "x"}', "index.js": "module.exports = 1\n"})
    h = depshash.deps_hash(work, "javascript")
    projects.add(db, "o/r", "javascript")
    projects.set_image(db, "o/r", projects.image_name("o/r", h), h)
    t = TicketSpec(ticket_id="o/r#1", title="t", body="b", repo="o/r", language="python", base_branch="other",
                   issue_number=1)
    out = _ingest(tmp_path, db, t, monkeypatch)
    assert "halt_reason" not in out, out
    assert out["runner_image"] == projects.image_name("o/r", h)
    assert (out["ticket"].language, out["ticket"].base_branch) == ("javascript", "main")   # project wins

    # someone adds a dependency on main: the next run stops before any model call
    subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "dep",
                    "--allow-empty"], check=True)
    (work / "package.json").write_text('{"name": "x", "dependencies": {"left-pad": "1"}}')
    subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "dep"],
                   check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", str(tmp_path / "origin" / "o" / "r.git"), "main"],
                   check=True)
    out = _ingest(tmp_path, db, t, monkeypatch)
    assert "dependencies changed" in out["halt_reason"] and "make project-rebuild REPO=o/r" in out["halt_reason"]


@needs_db
def test_qualification_runs_need_no_project(db, tmp_path, monkeypatch):
    _origin(tmp_path, {"app.py": "x = 1\n"})
    t = TicketSpec(ticket_id="QUAL-1", title="t", body="b", repo="o/r", language="python")
    out = _ingest(tmp_path, db, t, monkeypatch, qualification=True)
    assert "halt_reason" not in out and out["runner_image"] is None


@needs_db
def test_deps_archive_carries_only_dependency_files_and_the_image_name(db, tmp_path, monkeypatch):
    _origin(tmp_path, {"requirements.txt": "pytest\n", "app.py": "x = 1\n", "secret.env": "TOKEN=x\n"})
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{tmp_path}/origin/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:https")
    p = projects.add(db, "o/r", "python")
    raw = projects.deps_archive("o/r", p, tmp_path / "runs", {})
    t = tarfile.open(fileobj=io.BytesIO(raw))
    assert sorted(t.getnames()) == [".deps-hash", ".image", ".language", "requirements.txt"]
    h = t.extractfile(".deps-hash").read().decode()
    assert t.extractfile(".image").read().decode() == projects.image_name("o/r", h)
    assert not (tmp_path / "runs" / "_deps").exists() or not any((tmp_path / "runs" / "_deps").iterdir())


@needs_db
def test_serve_rejects_unregistered_tickets_and_holds_unready_ones(db, tmp_path):
    import json

    from factory.runner import _next_ticket
    inbox = tmp_path / "inbox"
    (inbox / "rejected").mkdir(parents=True)
    projects.add(db, "o/ready", "python")
    h = "c" * 64
    projects.set_image(db, "o/ready", projects.image_name("o/ready", h), h)
    projects.add(db, "o/unbuilt", "python")
    for name, repo in (("1-stranger", "x/y"), ("2-unbuilt", "o/unbuilt"), ("3-ready", "o/ready")):
        (inbox / f"{name}.json").write_text(json.dumps({"ticket_id": name, "title": "t", "body": "b", "repo": repo}))
        time.sleep(0.01)

    class D:
        dsn = db
    assert _next_ticket(D(), inbox) == inbox / "3-ready.json"
    assert (inbox / "rejected" / "1-stranger.json").exists()
    assert (inbox / "2-unbuilt.json").exists()                  # waits until its image is built


@needs_db
def test_every_language_can_be_registered_and_the_database_refuses_others(db):
    """Migration 007: the projects table accepts each runner family, and still refuses unknown languages
    even if a caller skips the Python check."""
    import psycopg
    for i, lang in enumerate(depshash.LANGUAGES):
        assert projects.add(db, f"o/lang{i}", lang).language == lang
    with psycopg.connect(db, autocommit=True) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("INSERT INTO projects (repo, language) VALUES ('o/go', 'go')")



@needs_db
def test_ingest_reads_the_acceptance_criteria_a_person_wrote(db, tmp_path, monkeypatch):
    """Gate 1: criteria come from the issue body, through the same sanitising as the rest of it."""
    work = _origin(tmp_path, {"package.json": '{"name": "x"}', "index.js": "module.exports = 1\n"})
    h = depshash.deps_hash(work, "node")
    projects.add(db, "o/r", "node")
    projects.set_image(db, "o/r", projects.image_name("o/r", h), h)
    body = ("Add shout.\n\n## Acceptance criteria\n- <b>shout</b>(\"ada\") returns \"ADA\"\n"
            "- token=abc123 is never logged\n- feel: it reads like a shout\n")
    t = TicketSpec(ticket_id="o/r#2", title="t", body=body, repo="o/r", language="node", issue_number=2)
    out = _ingest(tmp_path, db, t, monkeypatch)
    assert "halt_reason" not in out, out
    assert out["ticket"].criteria[0] == 'shout("ada") returns "ADA"'          # tags stripped
    assert "abc123" not in out["ticket"].criteria[1]                           # secrets redacted
    assert out["ticket"].manual_checks == ["it reads like a shout"]
