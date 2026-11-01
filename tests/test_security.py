"""Regression tests for the guardrail security review (README: "Isolation: how the factory protects itself").

Each test replays an attack that worked against the factory before the sealed runner, and checks it
no longer does. The last test runs a real sandbox through Docker and is skipped when Docker or the
test runner image is missing (see the docstring there).
"""
import base64
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sandbox_broker"))

import archive  # noqa: E402  (sandbox_broker/archive.py, standard library only)
from factory import patches  # noqa: E402
from factory.config import is_factory_repo, redact  # noqa: E402
from factory.nodes import classify_failure, write_results  # noqa: E402
from factory.state import FailureClass  # noqa: E402


def tar_of(entries) -> bytes:
    """entries: (name, kind, payload) with kind file | dir | symlink | hardlink | fifo."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as t:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(payload)
                t.addfile(info, io.BytesIO(payload))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                         "fifo": tarfile.FIFOTYPE}[kind]
            info.linkname = payload or ""
            t.addfile(info)
    return out.getvalue()


def make_repo(tmp_path) -> tuple[pathlib.Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    g("init", "-q")
    (repo / "app.js").write_text("module.exports = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "a.test.js").write_text("test('x', () => {})\n")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    return repo, g("rev-parse", "HEAD").stdout.decode().strip()


# ------------------------------------------------------------------ F1: the runner never gets .git
def test_runner_source_has_no_git_metadata_tests_or_symlinks(tmp_path):
    repo, _ = make_repo(tmp_path)
    (repo / "new.js").write_text("module.exports = 2\n")                         # new, untracked file
    (repo / "evil-link").symlink_to("/run/secrets/github_token")
    names = tarfile.open(fileobj=io.BytesIO(patches.source_archive(repo))).getnames()
    assert sorted(names) == ["app.js", "new.js"]


def test_planted_hooks_and_fsmonitor_do_not_run_in_factory_git_calls(tmp_path):
    """F1. Even if .git were exposed again, the factory's git calls ignore hooks and fsmonitor."""
    repo, base = make_repo(tmp_path)
    proof = tmp_path / "PWNED"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho hook >> {proof}\n")
    hook.chmod(0o755)
    (repo / ".git" / "hooks" / "pre-push").write_text(f"#!/bin/sh\necho push-hook >> {proof}\n")
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", f"echo fsmonitor >> {proof}; false"],
                   check=True)
    patches.reset_to_base(repo, base)
    (repo / "app.js").write_text("module.exports = 3\n")
    env = {"GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@l", "GIT_COMMITTER_NAME": "f", "GIT_COMMITTER_EMAIL": "f@l"}
    for args in (["checkout", "-B", "factory/x"], ["add", "-A"], ["commit", "--no-verify", "-m", "t"]):
        assert patches.git(repo, *args, check=False, env=env).returncode == 0
    assert not proof.exists()


# ------------------------------------------------------------------ F2: no symlinked or oversized results
def test_broker_rebuilds_incoming_archives_and_refuses_links_and_git():
    ok = archive.rebuild_archive(tar_of([("./src", "dir", None), ("./src/a.js", "file", b"x")]))
    assert tarfile.open(fileobj=io.BytesIO(ok)).getnames() == ["src", "src/a.js"]
    for bad in ([("link", "symlink", "/run/secrets/x")], [("h", "hardlink", "etc/passwd")],
                [("p", "fifo", None)], [(".git/config", "file", b"[core]")], [("a/.git/hooks/x", "file", b"")],
                [("/etc/x", "file", b"")], [("../x", "file", b"")], [("a/../../x", "file", b"")]):
        with pytest.raises(archive.Rejected):
            archive.rebuild_archive(tar_of(bad))
    with pytest.raises(archive.Rejected):
        archive.changed_files_archive(["../../etc/passwd"])


def test_broker_keeps_only_known_regular_result_files_under_the_limit():
    big = b"x" * (archive.MAX_RESULT_FILE + 1)
    stream = tar_of([(".factory", "dir", None),
                     (".factory/test.log", "symlink", "/run/secrets/claude_oauth_token"),
                     (".factory/lint.json", "file", big),
                     (".factory/sub/qa.json", "file", b"nested"),
                     (".factory/notes.txt", "file", b"unknown name"),
                     (".factory/result.json", "file", b'{"test_exit": 1}')])
    assert archive.read_results([stream[:1000], stream[1000:]]) == {"result.json": '{"test_exit": 1}'}


def test_factory_writes_result_files_itself(tmp_path):
    out = write_results(tmp_path / "results", {"test.log": "boom", "../../escape": "x", "qa.json": 5})
    assert sorted(p.name for p in out.iterdir()) == ["test.log"]
    assert not (out / "test.log").is_symlink()
    assert classify_failure(out, {"exit_code": 1})[0] is FailureClass.ASSERTION


@pytest.mark.parametrize("text", [
    'GITHUB_TOKEN=ghp_abcdefghijklmnop',
    'MY_SERVICE_SECRET=abcdefghijklmnop',
    '{"token": "sk-ant-oat01-abcdefghijkl", "created_epoch": 1}',
    'Authorization: Bearer ghp_abcdefghijklmnop',
    'stray sk-ant-oat01-abcdefghijklmnop in a log',
])
def test_redaction_covers_the_shapes_the_old_regex_missed(text):
    assert "abcdefghijkl" not in redact(text)


def test_redaction_removes_the_literal_secret_values(tmp_path, monkeypatch):
    (tmp_path / "github_token").write_text("plainvalue-without-any-prefix-123")
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    b64 = base64.b64encode(b"x-access-token:plainvalue-without-any-prefix-123").decode()
    out = redact(f"value plainvalue-without-any-prefix-123 and header basic-ish {b64}")
    assert "plainvalue" not in out and b64 not in out


# ------------------------------------------------------------------ the factory never works on itself
def test_factory_refuses_its_own_repo(monkeypatch):
    from factory.github import validate
    from factory.nodes import ingest
    from factory.state import TicketSpec
    assert is_factory_repo("Ethanoic/Software-Factory")
    with pytest.raises(SystemExit, match="factory's own repo"):
        validate({"repos": [{"repo": "ethanoic/software-factory"}]})
    t = TicketSpec(ticket_id="x", title="t", body="b", repo="ethanoic/software-factory", language="python")
    assert "refused" in ingest({"ticket": t, "run_id": "r"}, deps=None)["halt_reason"]


# ------------------------------------------------------------------ the compose file keeps the shape
def test_only_the_docker_proxy_holds_the_docker_socket_and_runs_stay_private():
    svc = yaml.safe_load((ROOT / "compose.factory.yml").read_text())["services"]
    for name, s in svc.items():
        mounts = [v if isinstance(v, str) else v.get("source", "") for v in s.get("volumes", [])]
        has_sock = any("docker.sock" in m for m in mounts)
        assert has_sock == (name == "docker-proxy"), name
    broker = svc["sandbox-broker"]
    assert not broker.get("volumes") and "HOST_WORKSPACE_ROOT" not in str(broker)
    assert svc["docker-proxy"]["environment"]["EXEC"] == 0
    for name in ("orchestrator", "pool-worker"):
        assert any(v.startswith("factory-runs:/runs") for v in svc[name]["volumes"])
    src = (ROOT / "sandbox_broker" / "app.py").read_text()
    assert '"bind"' in src and "HOST_ROOT" not in src and "/workspace\": {\"bind\"" not in src


# ------------------------------------------------------------------ live: a real sealed runner
DOCKER_IMAGE = os.environ.get("FACTORY_TEST_RUNNER_IMAGE", "factory-runner-test:local")


def _docker_ready() -> bool:
    try:
        import docker
        c = docker.from_env()
        c.images.get(DOCKER_IMAGE)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _docker_ready(), reason=f"needs Docker and the image {DOCKER_IMAGE} "
                    "(make test-sandbox builds it)")
def test_sealed_runner_end_to_end(tmp_path, monkeypatch):
    """The fake runner (tests/runner-probe) probes its sandbox and plants every result trick
    from the review. The broker must report a sealed sandbox and return only safe result files."""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("RUNNER_IMAGE_TS", DOCKER_IMAGE)
    sys.modules.pop("app", None)
    import app as broker
    broker.IMAGES["javascript"] = DOCKER_IMAGE

    repo, _ = make_repo(tmp_path)
    (repo / "evil-link").symlink_to("/etc/passwd")
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    (frozen / "a.test.js").write_text("test('x', () => {})\n")
    r = TestClient(broker.app).post("/run", json={
        "run_id": "sec-test-0001", "language": "javascript",
        "source_tar_b64": base64.b64encode(patches.source_archive(repo)).decode(),
        "tests_tar_b64": base64.b64encode(patches.dir_archive(frozen)).decode(),
        "changed_files": ["app.js"]})
    assert r.status_code == 200, r.text
    body = r.json()
    files = body["files"]
    assert set(files) == {"result.json", "junit.xml"}          # symlinked log, 2 MB lint, nested file dropped
    probe = json.loads(files["result.json"])
    assert probe == {"net": "blocked", "git": "absent", "tests_ro": "yes", "src": "ok", "link": "absent",
                     "frozen": "ok", "changed": "app.js"}
    import docker
    c = docker.from_env()
    assert not c.containers.list(all=True, filters={"label": "factory.run_id=sec-test-0001"})
    assert not c.volumes.list(filters={"label": "factory.run_id=sec-test-0001"})


# ------------------------------------------------------------------ cleanup keeps what a run still needs
@pytest.mark.skipif(not os.environ.get("FACTORY_TEST_DSN"), reason="set FACTORY_TEST_DSN")
def test_runs_clean_keeps_active_runs(tmp_path):
    import psycopg
    from factory.board_state import record_run
    from factory.runner import clean, migrate
    dsn = os.environ["FACTORY_TEST_DSN"]
    migrate(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("TRUNCATE runs")
    for run, st in (("old-done", "done"), ("old-waiting", "awaiting_approval"), ("new-done", "done")):
        record_run(dsn, run, ticket_id="t", repo="o/r", status=st)
        (tmp_path / run / "frozen").mkdir(parents=True)
        (tmp_path / run / "frozen" / "a.py").write_text("x")
        (tmp_path / run / "frozen" / "a.py").chmod(0o444)
    old = 1_000_000_000
    for run in ("old-done", "old-waiting"):
        os.utime(tmp_path / run, (old, old))
    assert clean(dsn, tmp_path, days=14) == ["old-done"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["new-done", "old-waiting"]
