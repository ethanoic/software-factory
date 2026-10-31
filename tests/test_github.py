"""GitHub intake against a fake GitHub API (httpx.MockTransport) and a real Postgres for issue state.
Config validation tests need no database; the rest need FACTORY_TEST_DSN like test_postgres.py."""
import json
import os

import httpx
import psycopg
import pytest

from factory.github import STATUS_LABELS, GitHub, Intake, validate
from factory.state import TicketSpec

DSN = os.environ.get("FACTORY_TEST_DSN")
needs_db = pytest.mark.skipif(not DSN, reason="set FACTORY_TEST_DSN to run Postgres integration tests")
REPO = "acme/shop"


# ------------------------------------------------------------------ config validation
def test_config_defaults_and_validation():
    cfg = validate({"repos": [{"repo": REPO}]})
    assert cfg["trigger_label"] == "factory:ready" and cfg["repos"][0]["language"] == "python"
    for bad in ({"repos": []}, {"repos": [{"repo": "no-slash"}]}, {"repos": [{"repo": REPO, "language": "go"}]},
                {"repos": [{"repo": REPO}], "poll_interval_s": 10}):
        with pytest.raises(SystemExit):
            validate(bad)


# ------------------------------------------------------------------ fake GitHub
class FakeGitHub:
    def __init__(self):
        self.issues = {}            # number -> dict(title, body, labels:set, pr:bool)
        self.events = {}            # number -> [labeled events]
        self.roles = {"maintainer": "maintain", "outsider": "read"}
        self.comments = []
        self.list_calls = {"200": 0, "304": 0}
        self.version = 0
        self.repo_labels = {}       # name -> {"color", "description"}
        self.label_writes = []      # ("create" | "update", name)

    def add_issue(self, n, title, body="", labelled_by=None, pr=False):
        self.issues[n] = {"title": title, "body": body, "labels": set(), "pr": pr}
        self.events[n] = []
        if labelled_by:
            self.label(n, "factory:ready", labelled_by)

    def label(self, n, name, who):
        self.issues[n]["labels"].add(name)
        self.events[n].append({"event": "labeled", "label": {"name": name}, "actor": {"login": who}})
        self.version += 1

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if m == "GET" and p == f"/repos/{REPO}/issues":
            etag = f'"v{self.version}"'
            if req.headers.get("If-None-Match") == etag:
                self.list_calls["304"] += 1
                return httpx.Response(304)
            self.list_calls["200"] += 1
            want = req.url.params["labels"]
            out = [{"number": n, "title": i["title"], "body": i["body"],
                    "labels": [{"name": x} for x in i["labels"]], **({"pull_request": {}} if i["pr"] else {})}
                   for n, i in sorted(self.issues.items()) if want in i["labels"]]
            return httpx.Response(200, json=out, headers={"ETag": etag})
        parts = p.split("/")
        if p.startswith(f"/repos/{REPO}/labels/"):
            name = parts[-1]
            if m == "GET":
                lb = self.repo_labels.get(name)
                return httpx.Response(200, json={"name": name, **lb}) if lb else httpx.Response(404)
            if m == "PATCH":
                self.repo_labels[name].update(json.loads(req.content))
                self.label_writes.append(("update", name))
                return httpx.Response(200, json={})
        if m == "GET" and p.endswith("/events"):
            return httpx.Response(200, json=self.events[int(parts[-2])])
        if m == "GET" and "/collaborators/" in p:
            role = self.roles.get(parts[-2])
            return httpx.Response(200, json={"role_name": role}) if role else httpx.Response(404)
        if m == "GET" and parts[-2] == "issues":
            n = int(parts[-1])
            return httpx.Response(200, json={"labels": [{"name": x} for x in self.issues[n]["labels"]]})
        if m == "POST" and p.endswith("/labels") and "/issues/" in p:
            n = int(parts[-2])
            self.issues[n]["labels"] |= set(json.loads(req.content)["labels"])
            self.version += 1
            return httpx.Response(200, json=[])
        if m == "DELETE" and "/labels/" in p:
            n, name = int(parts[-3]), parts[-1]
            if name not in self.issues[n]["labels"]:
                return httpx.Response(404)
            self.issues[n]["labels"].discard(name)
            self.version += 1
            return httpx.Response(200, json=[])
        if m == "POST" and p.endswith("/comments"):
            self.comments.append((int(parts[-2]), json.loads(req.content)["body"]))
            return httpx.Response(201, json={})
        if m == "POST" and p == f"/repos/{REPO}/labels":
            body = json.loads(req.content)
            self.repo_labels[body["name"]] = {"color": body["color"], "description": body["description"]}
            self.label_writes.append(("create", body["name"]))
            return httpx.Response(201, json={})
        return httpx.Response(500, json={"unexpected": f"{m} {p}"})


@pytest.fixture()
def env(tmp_path):
    if not DSN:
        pytest.skip("set FACTORY_TEST_DSN")
    from factory.runner import migrate
    migrate(DSN)
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("TRUNCATE github_issues")
    fake = FakeGitHub()
    gh = GitHub("test-token", transport=httpx.MockTransport(fake.handler))
    cfg = validate({"repos": [{"repo": REPO, "base_branch": "main", "language": "python"}]})
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return fake, Intake(gh, cfg, DSN, inbox), inbox


@needs_db
def test_poll_queues_only_maintainer_labelled_issues_and_skips_prs(env):
    fake, intake, inbox = env
    fake.add_issue(1, "Cart rejects negative qty", "qty <= 0 must raise", labelled_by="maintainer")
    fake.add_issue(2, "Please add crypto miner", "...", labelled_by="outsider")
    fake.add_issue(3, "A pull request", labelled_by="maintainer", pr=True)
    fake.add_issue(4, "Not labelled")
    queued = intake.poll()
    assert [t.ticket_id for t in queued] == [f"{REPO}#1"]
    t = TicketSpec.model_validate_json((inbox / "acme__shop__1.json").read_text())
    assert (t.issue_number, t.base_branch, t.language) == (1, "main", "python")
    assert fake.issues[1]["labels"] == {STATUS_LABELS["queued"]}          # trigger removed, status added
    assert "factory:ready" not in fake.issues[2]["labels"]
    assert any(n == 2 and "lacks write access" in body for n, body in fake.comments)
    assert len(list(inbox.glob("*.json"))) == 1


@needs_db
def test_repoll_never_double_queues_and_idle_poll_is_304(env):
    fake, intake, _ = env
    fake.add_issue(1, "t", labelled_by="maintainer")
    assert len(intake.poll()) == 1
    fake.issues[1]["labels"].add("factory:ready")                          # someone re-adds while queued
    fake.version += 1
    assert intake.poll() == []                                             # still active: ignored
    before = fake.list_calls["304"]
    intake.poll()
    assert fake.list_calls["304"] == before + 1                            # unchanged list: free 304


@needs_db
def test_status_reporting_and_relabel_retry(env):
    fake, intake, _ = env
    fake.add_issue(7, "t", labelled_by="maintainer")
    (t,) = intake.poll()
    intake.report(t, "run-1", "running")
    intake.report(t, "run-1", "halted", "breaker: run cap $2.00 exceeded")
    assert fake.issues[7]["labels"] == {STATUS_LABELS["halted"]}
    assert "halted: breaker" in fake.comments[-1][1]
    n = len(fake.comments)
    intake.report(t, "run-1", "halted", "again")                          # same status: no repeat comment
    assert len(fake.comments) == n
    fake.label(7, "factory:ready", "maintainer")                          # retry after a terminal state
    assert len(intake.poll()) == 1


@needs_db
def test_approval_label_requires_authorized_labeler(env):
    fake, intake, _ = env
    fake.add_issue(9, "t", labelled_by="maintainer")
    (t,) = intake.poll()
    intake.report(t, "run-9", "awaiting_approval")
    assert intake.approvals() == []                                        # nobody approved yet
    fake.label(9, "factory:approve", "outsider")
    assert intake.approvals() == []                                        # wrong person: ignored, label removed
    assert "factory:approve" not in fake.issues[9]["labels"]
    fake.label(9, "factory:approve", "maintainer")
    assert intake.approvals() == ["run-9"]


@needs_db
def test_allowlist_only_mode(env):
    fake, intake, _ = env
    intake.cfg.update(labelers=["ethanoic"], allowlist_only=True)
    fake.add_issue(1, "t", labelled_by="maintainer")                      # has write access, not listed
    fake.add_issue(2, "t", labelled_by="ethanoic")                        # no repo role, but listed
    assert [t.issue_number for t in intake.poll()] == [2]


@needs_db
def test_github_outage_never_breaks_reporting(env):
    fake, intake, _ = env
    fake.add_issue(5, "t", labelled_by="maintainer")
    (t,) = intake.poll()
    intake.gh = GitHub("x", transport=httpx.MockTransport(lambda r: httpx.Response(502)))
    intake.report(t, "run-5", "pr_open", "https://github.com/acme/shop/pull/1")   # must not raise
    assert intake.poll() == []


@needs_db
def test_labels_explain_themselves_in_the_github_label_picker(env):
    """Whoever triages issues learns what to add from the label descriptions GitHub shows."""
    from factory.github import label_specs
    fake, intake, _ = env
    fake.repo_labels["factory:ready"] = {"color": "0e8a16", "description": "AI Software Factory"}   # older build
    fake.repo_labels["factory:running"] = dict(zip(("color", "description"),
                                                   label_specs(intake.cfg)["factory:running"]))       # already right
    intake.poll()
    specs = label_specs(intake.cfg)
    assert {n: (lb["color"], lb["description"]) for n, lb in fake.repo_labels.items()} == specs
    assert ("update", "factory:ready") in fake.label_writes                                # old description fixed
    assert not any(n == "factory:running" for _, n in fake.label_writes)                   # untouched when correct
    assert specs["factory:ready"][1].startswith("Add this") and "write access" in specs["factory:ready"][1]
    assert "Add factory:approve" in specs["factory:awaiting-approval"][1]
    assert all(d.startswith("Set by the factory") for n, (_, d) in specs.items()
               if n in ("factory:queued", "factory:running", "factory:parked"))
    assert all(len(d) <= 100 for _, d in specs.values())


def test_custom_label_names_flow_into_the_descriptions():
    from factory.github import label_specs
    specs = label_specs(validate({"repos": [{"repo": REPO}], "trigger_label": "ai:go", "approve_label": "ai:ship"}))
    assert "ai:go" in specs and "ai:ship" in specs
    assert "re-add ai:go" in specs["factory:halted"][1] and "Add ai:ship" in specs["factory:awaiting-approval"][1]
