"""GitHub intake (spec §5.6): poll configured repos for labelled issues, report status back.

Polling, not webhooks: the Mac has no public endpoint. Conditional requests (ETag) make an idle poll
free: an unchanged list returns 304, which does not count against the rate limit.

Only issues whose trigger label was applied by someone with write access (or on the `labelers`
allowlist) are taken, so strangers cannot spend the Claude plan or inject text into the pipeline.
"""
from __future__ import annotations

import functools
import json
import os
import pathlib
import re
import time

import httpx
import psycopg
import yaml

from factory.state import TicketSpec

API = "https://api.github.com"
TRUSTED_ROLES = {"admin", "maintain", "write"}
ACTIVE = ("queued", "running", "awaiting_approval", "parked")
STATUS_LABELS = {
    "queued": "factory:queued", "running": "factory:running",
    "awaiting_approval": "factory:awaiting-approval", "pr_open": "factory:pr-open",
    "halted": "factory:halted", "rejected": "factory:rejected", "parked": "factory:parked",
}
LABEL_COLORS = {"factory:ready": "0e8a16", "factory:approve": "5319e7"}
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@functools.lru_cache(maxsize=1)
def config() -> dict | None:
    """github.yaml, or None when GitHub intake is not configured (inbox-only mode)."""
    path = pathlib.Path(os.environ.get("GITHUB_CONFIG", "/app/github.yaml"))
    if not path.is_file():                      # a missing bind-mount source becomes a directory
        return None
    cfg = yaml.safe_load(path.read_text()) or {}
    if not cfg.get("enabled", True):
        return None
    return validate(cfg)


def validate(cfg: dict) -> dict:
    cfg.setdefault("poll_interval_s", 120)
    cfg.setdefault("trigger_label", "factory:ready")
    cfg.setdefault("approve_label", "factory:approve")
    cfg.setdefault("labelers", [])
    cfg.setdefault("comment_on_status", True)
    if int(cfg["poll_interval_s"]) < 60:
        raise SystemExit("github.yaml: poll_interval_s must be >= 60")
    repos = cfg.get("repos") or []
    if not repos:
        raise SystemExit("github.yaml: list at least one repo under `repos`")
    for r in repos:
        if not REPO_RE.fullmatch(r.get("repo", "")):
            raise SystemExit(f"github.yaml: invalid repo {r.get('repo')!r} (expected owner/name)")
        r.setdefault("base_branch", "main")
        if r.setdefault("language", "python") not in ("python", "javascript", "typescript"):
            raise SystemExit(f"github.yaml: {r['repo']} language must be python, javascript or typescript")
    return cfg


class GitHub:
    """Minimal REST client: ETag caching for polls, tolerant label operations."""

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.c = httpx.Client(base_url=API, timeout=30, transport=transport, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ai-software-factory"})
        self._etag: dict[str, str] = {}
        self._cache: dict[str, object] = {}

    def get(self, path: str, params: dict | None = None, *, conditional: bool = False):
        """Returns (json, changed). With conditional=True an unchanged resource returns (cached, False)."""
        key = path + "?" + json.dumps(params or {}, sort_keys=True)
        headers = {"If-None-Match": self._etag[key]} if conditional and key in self._etag else {}
        r = self.c.get(path, params=params, headers=headers)
        if r.status_code == 304:
            return self._cache[key], False
        r.raise_for_status()
        data = r.json()
        if conditional and r.headers.get("ETag"):
            self._etag[key], self._cache[key] = r.headers["ETag"], data
        return data, True

    def labeler(self, repo: str, number: int, label: str) -> str | None:
        """Login of whoever most recently applied `label` to the issue."""
        who, page = None, 1
        while page <= 10:
            events, _ = self.get(f"/repos/{repo}/issues/{number}/events", {"per_page": 100, "page": page})
            for e in events:
                if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label:
                    who = (e.get("actor") or {}).get("login")
            if len(events) < 100:
                break
            page += 1
        return who

    def role(self, repo: str, login: str) -> str | None:
        r = self.c.get(f"/repos/{repo}/collaborators/{login}/permission")
        if r.status_code != 200:
            return None                         # not a collaborator, or token lacks Metadata: read
        j = r.json()
        return j.get("role_name") or j.get("permission")

    def labels_of(self, repo: str, number: int) -> set[str]:
        issue, _ = self.get(f"/repos/{repo}/issues/{number}")
        return {lb["name"] for lb in issue.get("labels", [])}

    def add_label(self, repo: str, number: int, name: str) -> None:
        self.c.post(f"/repos/{repo}/issues/{number}/labels", json={"labels": [name]}).raise_for_status()

    def remove_label(self, repo: str, number: int, name: str) -> None:
        r = self.c.delete(f"/repos/{repo}/issues/{number}/labels/{name}")
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def comment(self, repo: str, number: int, body: str) -> None:
        self.c.post(f"/repos/{repo}/issues/{number}/comments", json={"body": body}).raise_for_status()

    def ensure_labels(self, repo: str, names: list[str]) -> None:
        for n in names:
            if self.c.get(f"/repos/{repo}/labels/{n}").status_code == 404:
                self.c.post(f"/repos/{repo}/labels", json={"name": n, "color": LABEL_COLORS.get(n, "c5def5"),
                                                            "description": "AI Software Factory"})


class Intake:
    """Polls issues into the inbox and mirrors run status back onto them."""

    def __init__(self, gh: GitHub, cfg: dict, dsn: str, inbox: pathlib.Path):
        self.gh, self.cfg, self.dsn, self.inbox = gh, cfg, dsn, inbox
        self._last_poll = 0.0
        self._labels_ready: set[str] = set()

    # ---------------------------------------------------------- authorization
    def authorized(self, repo: str, login: str | None) -> bool:
        if not login:
            return False
        if login in self.cfg["labelers"]:
            return True
        if self.cfg.get("allowlist_only"):
            return False
        return (self.gh.role(repo, login) or "") in TRUSTED_ROLES

    # ---------------------------------------------------------- state table
    def _status(self, repo: str, number: int) -> str | None:
        with psycopg.connect(self.dsn) as c:
            r = c.execute("SELECT status FROM github_issues WHERE repo=%s AND number=%s", (repo, number)).fetchone()
        return r[0] if r else None

    def _record(self, repo: str, number: int, status: str, run_id: str | None = None) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("""INSERT INTO github_issues (repo, number, status, run_id) VALUES (%s,%s,%s,%s)
                         ON CONFLICT (repo, number) DO UPDATE SET status = EXCLUDED.status,
                           run_id = coalesce(EXCLUDED.run_id, github_issues.run_id), updated_at = now()""",
                      (repo, number, status, run_id))

    # ---------------------------------------------------------- polling
    def due(self) -> bool:
        return time.time() - self._last_poll >= int(self.cfg["poll_interval_s"])

    def poll(self) -> list[TicketSpec]:
        """One pass over every repo. Writes accepted issues into the inbox and returns their tickets."""
        self._last_poll = time.time()
        trigger = self.cfg["trigger_label"]
        queued = []
        for r in self.cfg["repos"]:
            repo = r["repo"]
            try:
                if repo not in self._labels_ready:
                    self.gh.ensure_labels(repo, [trigger, self.cfg["approve_label"], *STATUS_LABELS.values()])
                    self._labels_ready.add(repo)
                issues, changed = self.gh.get(f"/repos/{repo}/issues", {
                    "state": "open", "labels": trigger, "sort": "created", "direction": "asc", "per_page": 50},
                    conditional=True)
            except httpx.HTTPError as e:
                print(f"[github] poll {repo} failed: {e}", flush=True)
                continue
            if not changed:
                continue
            for it in issues:
                if "pull_request" in it:                         # the issues API also lists PRs
                    continue
                n = it["number"]
                if self._status(repo, n) in ACTIVE:
                    continue
                who = self.gh.labeler(repo, n, trigger)
                self.gh.remove_label(repo, n, trigger)           # re-adding it later means "run again"
                if not self.authorized(repo, who):
                    self._record(repo, n, "unauthorized")
                    self.gh.comment(repo, n, f"The factory ignored this: `{trigger}` was applied by "
                                             f"@{who or 'unknown'}, who lacks write access. A maintainer can re-apply it.")
                    continue
                t = TicketSpec(ticket_id=f"{repo}#{n}", title=it.get("title") or "", body=it.get("body") or "",
                               repo=repo, base_branch=r["base_branch"], language=r["language"], issue_number=n)
                name = f"{repo.replace('/', '__')}__{n}.json"
                (self.inbox / name).write_text(t.model_dump_json())
                self._record(repo, n, "queued")
                self._set_label(repo, n, "queued")
                queued.append(t)
                print(f"[github] queued {repo}#{n} (labelled by @{who})", flush=True)
        return queued

    def approvals(self) -> list[str]:
        """Run ids whose issue got the approve label from an authorized user."""
        label = self.cfg["approve_label"]
        with psycopg.connect(self.dsn) as c:
            rows = c.execute("SELECT repo, number, run_id FROM github_issues WHERE status='awaiting_approval'").fetchall()
        out = []
        for repo, n, run_id in rows:
            try:
                if label not in self.gh.labels_of(repo, n):
                    continue
                who = self.gh.labeler(repo, n, label)
                self.gh.remove_label(repo, n, label)
                if self.authorized(repo, who):
                    out.append(run_id)
                else:
                    self.gh.comment(repo, n, f"`{label}` was applied by @{who or 'unknown'}, who lacks write access; ignored.")
            except httpx.HTTPError as e:
                print(f"[github] approval check {repo}#{n} failed: {e}", flush=True)
        return out

    # ---------------------------------------------------------- reporting
    def _set_label(self, repo: str, n: int, status: str) -> None:
        for s, name in STATUS_LABELS.items():
            if s != status:
                self.gh.remove_label(repo, n, name)
        self.gh.add_label(repo, n, STATUS_LABELS[status])

    def report(self, ticket: TicketSpec, run_id: str, status: str, detail: str = "") -> None:
        """Mirror a run status onto its issue. Never raises: GitHub being down must not kill a run."""
        if ticket.issue_number is None:
            return
        repo, n = ticket.repo, ticket.issue_number
        try:
            if self._status(repo, n) == status:          # e.g. hourly park retries: no repeat comments
                return
            self._record(repo, n, status, run_id)
            self._set_label(repo, n, status)
            if self.cfg["comment_on_status"]:
                msg = {
                    "running": f"Factory run `{run_id}` started.",
                    "awaiting_approval": f"Tests and QA passed. Add `{self.cfg['approve_label']}` to open the draft PR.",
                    "pr_open": f"Draft PR opened: {detail}",
                    "halted": f"Factory run `{run_id}` halted: {detail}",
                    "rejected": f"Not taken by the factory: {detail}. Edit the issue and re-add "
                                f"`{self.cfg['trigger_label']}` to retry.",
                    "parked": "Paused: the Claude plan limit was reached. The run resumes automatically.",
                }.get(status)
                if msg:
                    self.gh.comment(repo, n, msg[:2000])
        except (httpx.HTTPError, psycopg.Error) as e:
            print(f"[github] report {repo}#{n} {status} failed: {e}", flush=True)


def intake_from_env(dsn: str, inbox: pathlib.Path) -> Intake | None:
    from factory.config import secret
    cfg = config()
    token = secret("github_token")
    if cfg is None or not token:
        return None
    return Intake(GitHub(token), cfg, dsn, inbox)
