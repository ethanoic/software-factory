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

from factory.config import is_factory_repo, redact
from factory.depshash import LANGUAGES
from factory.state import TicketSpec

API = "https://api.github.com"
TRUSTED_ROLES = {"admin", "maintain", "write"}
ACTIVE = ("queued", "running", "awaiting_criteria", "awaiting_arch_approval", "awaiting_approval", "parked")
STATUS_LABELS = {
    "queued": "factory:queued", "running": "factory:running", "awaiting_criteria": "factory:awaiting-criteria",
    "awaiting_approval": "factory:awaiting-approval", "pr_open": "factory:pr-open",
    "awaiting_arch_approval": "factory:awaiting-arch-approval",
    "halted": "factory:halted", "rejected": "factory:rejected", "parked": "factory:parked",
}
# Colours match the board: strong for the labels people add, yellow when it is your move,
# red when stopped, grey for status the factory manages.
GREEN, PURPLE, YELLOW, RED, GREY = "1f883d", "8250df", "fbca04", "d73a4a", "d0d7de"


def label_specs(cfg: dict) -> dict[str, tuple[str, str]]:
    """name -> (colour, description). GitHub shows the description under each label in the label picker,
    so this is where whoever triages issues learns what to add. Descriptions are capped at 100 chars."""
    ready, approve, crit, arch = cfg["trigger_label"], cfg["approve_label"], cfg["criteria_label"], cfg["arch_label"]
    rework = cfg["rework_label"]
    by_factory = "Set by the factory, don't change by hand:"
    specs = {
        ready: (GREEN, "Add this to have the AI factory work on the issue. Needs write access."),
        crit: (PURPLE, "Add this to approve the acceptance criteria, as they stand in the issue. Needs write access."),
        STATUS_LABELS["awaiting_criteria"]: (YELLOW, f"Check the proposed acceptance criteria, then add {crit}."),
        arch: (PURPLE, "Add this to push a change that alters the architecture, after reading why. Needs write access."),
        rework: (PURPLE, "Comment what's wrong (start with bug: or spec:), then add this to send the change back."),
        STATUS_LABELS["awaiting_arch_approval"]: (YELLOW, f"Checks passed; the change alters the architecture. Add {arch}."),
        approve: (PURPLE, "Add this to open the draft PR after the factory's tests pass. Needs write access."),
        STATUS_LABELS["queued"]: (GREY, f"{by_factory} waiting for a free slot."),
        STATUS_LABELS["running"]: (GREY, f"{by_factory} working on it now."),
        STATUS_LABELS["parked"]: (GREY, f"{by_factory} paused on the Claude limit. Resumes on its own."),
        STATUS_LABELS["awaiting_approval"]: (YELLOW, f"Tests passed. Add {approve} to open the draft PR."),
        STATUS_LABELS["pr_open"]: (YELLOW, "Draft PR open. Review and merge it; merging closes the issue."),
        STATUS_LABELS["halted"]: (RED, f"Stopped. Read the factory's comment, then re-add {ready} to retry."),
        STATUS_LABELS["rejected"]: (GREY, f"Not taken. Read the factory's comment; reword and re-add {ready} to retry."),
    }
    return {name: (colour, desc[:100]) for name, (colour, desc) in specs.items()}
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
    cfg.setdefault("criteria_label", "factory:criteria-ok")
    cfg.setdefault("arch_label", "factory:arch-approve")
    cfg.setdefault("rework_label", "factory:rework")
    cfg.setdefault("labelers", [])
    cfg.setdefault("comment_on_status", True)
    if int(cfg["poll_interval_s"]) < 60:
        raise SystemExit("github.yaml: poll_interval_s must be >= 60")
    # Projects live in the factory's database now (make project-add). A `repos` list here is only read
    # once, to move older setups over.
    repos = cfg.get("repos") or []
    for r in repos:
        if not REPO_RE.fullmatch(r.get("repo", "")):
            raise SystemExit(f"github.yaml: invalid repo {r.get('repo')!r} (expected owner/name)")
        if is_factory_repo(r["repo"]):
            raise SystemExit(f"github.yaml: {r['repo']} is the factory's own repo; the factory never works on "
                             "itself. Remove it from `repos` (change the factory by pull request instead).")
        r.setdefault("base_branch", "main")
        if r.setdefault("language", "python") not in LANGUAGES:
            raise SystemExit(f"github.yaml: {r['repo']} language must be one of {', '.join(LANGUAGES)}")
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

    def last_comment_by(self, repo: str, number: int, login: str) -> str | None:
        """The newest comment on an issue written by `login`, or None. Reads the newest 100 comments."""
        r = self.c.get(f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100, "sort": "created",
                                                                          "direction": "desc"})
        r.raise_for_status()
        mine = [c for c in r.json() if (c.get("user") or {}).get("login") == login]
        return max(mine, key=lambda c: c.get("created_at", ""))["body"] if mine else None

    def comment(self, repo: str, number: int, body: str) -> None:
        self.c.post(f"/repos/{repo}/issues/{number}/comments", json={"body": redact(body)}).raise_for_status()

    def ensure_labels(self, repo: str, specs: dict[str, tuple[str, str]]) -> None:
        """Create missing labels, and bring existing ones up to date (colour and description)."""
        for name, (colour, desc) in specs.items():
            r = self.c.get(f"/repos/{repo}/labels/{name}")
            if r.status_code == 404:
                self.c.post(f"/repos/{repo}/labels", json={"name": name, "color": colour, "description": desc})
            elif r.status_code == 200:
                cur = r.json()
                if (cur.get("color") or "").lower() != colour or (cur.get("description") or "") != desc:
                    self.c.patch(f"/repos/{repo}/labels/{name}", json={"color": colour, "description": desc})


class Intake:
    """Polls issues into the inbox and mirrors run status back onto them."""

    def __init__(self, gh: GitHub, cfg: dict, dsn: str, inbox: pathlib.Path, projects=None):
        """projects: callable returning the repos to poll, as dicts with repo, base_branch, language and
        labelers. Default: the ready projects in the factory's database."""
        self.gh, self.cfg, self.dsn, self.inbox = gh, cfg, dsn, inbox
        self._projects = projects
        self._last_poll = 0.0
        self._labels_ready: set[str] = set()

    def projects(self) -> list[dict]:
        if self._projects is not None:
            return list(self._projects())
        from factory import projects
        return [p.as_intake() for p in projects.ready(self.dsn)]

    # ---------------------------------------------------------- authorization
    def authorized(self, repo: str, login: str | None) -> bool:
        if not login:
            return False
        extra = next((p.get("labelers") or [] for p in self.projects() if p["repo"].lower() == repo.lower()), [])
        if login in self.cfg["labelers"] or login in extra:
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
        for r in self.projects():
            repo = r["repo"]
            try:
                if repo not in self._labels_ready:
                    self.gh.ensure_labels(repo, label_specs(self.cfg))
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
        self.sync_closed()
        return queued

    def sync_closed(self) -> int:
        """Mark runs finished once their issue closes (normally: you merged the PR, which says Closes #n)."""
        from factory.board_state import record_run
        with psycopg.connect(self.dsn) as c:
            rows = c.execute("SELECT repo, number, run_id FROM github_issues WHERE status = 'pr_open'").fetchall()
        closed = 0
        for repo, n, run_id in rows:
            try:
                issue, _ = self.gh.get(f"/repos/{repo}/issues/{n}", conditional=True)
            except httpx.HTTPError:
                continue
            if issue.get("state") == "closed":
                self._record(repo, n, "closed")
                record_run(self.dsn, run_id, status="done")
                closed += 1
        return closed

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

    REWORKABLE = ("pr_open", "awaiting_approval", "awaiting_arch_approval")

    def reworks(self) -> list[TicketSpec]:
        """Issues whose change a person sent back: the rework label from an authorized user, and that user's
        newest comment as the reason. Each becomes a new run of the same ticket, carrying the reason."""
        label = self.cfg["rework_label"]
        with psycopg.connect(self.dsn) as c:
            rows = c.execute("SELECT repo, number FROM github_issues WHERE status = ANY(%s)",
                             (list(self.REWORKABLE),)).fetchall()
        queued = []
        projects = {p["repo"].lower(): p for p in self.projects()}
        for repo, n in rows:
            try:
                if label not in self.gh.labels_of(repo, n):
                    continue
                who = self.gh.labeler(repo, n, label)
                self.gh.remove_label(repo, n, label)
                if not self.authorized(repo, who):
                    self.gh.comment(repo, n, f"`{label}` was applied by @{who or 'unknown'}, who lacks write access; ignored.")
                    continue
                reason = (self.gh.last_comment_by(repo, n, who) or "").strip()
                if not reason:
                    self.gh.comment(repo, n, f"Say what is wrong in a comment first (start it with `bug:` if the code "
                                             f"misses the criteria, `spec:` if the criteria were wrong), then add "
                                             f"`{label}` again.")
                    continue
                p = projects.get(repo.lower())
                if p is None:
                    continue
                issue, _ = self.gh.get(f"/repos/{repo}/issues/{n}")
                t = TicketSpec(ticket_id=f"{repo}#{n}", title=issue.get("title") or "", body=issue.get("body") or "",
                               repo=repo, base_branch=p["base_branch"], language=p["language"], issue_number=n,
                               rework=redact(reason)[:4000])
                (self.inbox / f"{repo.replace('/', '__')}__{n}__rework.json").write_text(t.model_dump_json())
                self._record(repo, n, "queued")
                self._set_label(repo, n, "queued")
                queued.append(t)
                print(f"[github] rework queued for {repo}#{n} (by @{who})", flush=True)
            except httpx.HTTPError as e:
                print(f"[github] rework check {repo}#{n} failed: {e}", flush=True)
        return queued

    def arch_approvals(self) -> list[str]:
        """Run ids waiting at Gate 3 whose issue got the architecture label from an authorized user. The
        ordinary approve label is not enough for these: it is removed with a comment saying why."""
        label, plain = self.cfg["arch_label"], self.cfg["approve_label"]
        with psycopg.connect(self.dsn) as c:
            rows = c.execute("SELECT repo, number, run_id FROM github_issues "
                             "WHERE status='awaiting_arch_approval'").fetchall()
        out = []
        for repo, n, run_id in rows:
            try:
                labels = self.gh.labels_of(repo, n)
                if plain in labels:
                    self.gh.remove_label(repo, n, plain)
                    self.gh.comment(repo, n, f"`{plain}` is not enough here: this change alters the architecture "
                                             f"(see the comment above). Add `{label}` once you have checked it.")
                if label not in labels:
                    continue
                who = self.gh.labeler(repo, n, label)
                self.gh.remove_label(repo, n, label)
                if self.authorized(repo, who):
                    out.append(run_id)
                else:
                    self.gh.comment(repo, n, f"`{label}` was applied by @{who or 'unknown'}, who lacks write access; ignored.")
            except httpx.HTTPError as e:
                print(f"[github] architecture approval check {repo}#{n} failed: {e}", flush=True)
        return out

    def criteria_approvals(self) -> list[tuple[str, "criteria.Criteria"]]:
        """(run id, criteria) for runs waiting at Gate 1 whose issue got the criteria label from an authorized
        user. The criteria are read from the issue as it stands now: if the person added or edited an
        Acceptance criteria section, that wins over the planner's proposal."""
        from factory import criteria
        label = self.cfg["criteria_label"]
        with psycopg.connect(self.dsn) as c:
            rows = c.execute("SELECT repo, number, run_id FROM github_issues WHERE status='awaiting_criteria'").fetchall()
        out = []
        for repo, n, run_id in rows:
            try:
                if label not in self.gh.labels_of(repo, n):
                    continue
                who = self.gh.labeler(repo, n, label)
                self.gh.remove_label(repo, n, label)
                if not self.authorized(repo, who):
                    self.gh.comment(repo, n, f"`{label}` was applied by @{who or 'unknown'}, who lacks write access; ignored.")
                    continue
                issue, _ = self.gh.get(f"/repos/{repo}/issues/{n}")
                found = criteria.parse(issue.get("body") or "")
                found = criteria.Criteria(testable=[redact(x) for x in found.testable],
                                          manual=[redact(x) for x in found.manual])
                out.append((run_id, found))
            except httpx.HTTPError as e:
                print(f"[github] criteria check {repo}#{n} failed: {e}", flush=True)
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
                    "awaiting_criteria": (
                        "No acceptance criteria were found in this issue, so the planner proposed these. Nothing "
                        "is tested until a person approves them.\n\n```markdown\n" + detail + "\n```\n\n"
                        "If they are right, add `" + self.cfg["criteria_label"] + "`. If not, paste the section "
                        "into the issue description, edit it (one bullet per criterion; start a bullet with "
                        "`feel:` for anything only a person can judge), then add `" + self.cfg["criteria_label"]
                        + "`. The section in the issue wins."),
                    "awaiting_approval": f"Tests and QA passed.\n\n{detail}\n\nRead the change first: on the factory "
                                         f"computer, run `make diff RUN={run_id}`. Then add "
                                         f"`{self.cfg['approve_label']}` to open the draft PR.",
                    "awaiting_arch_approval": f"Tests, QA and the independent review passed, but this change "
                                              f"alters the architecture.\n\n{detail}\n\nRead the change first: on the "
                                              f"factory computer, run `make diff RUN={run_id}`. To push it, add "
                                              f"`{self.cfg['arch_label']}` (`{self.cfg['approve_label']}` is not "
                                              f"enough for this one).",
                    "pr_open": f"Draft PR opened: {detail}\n\nNot right? Comment what's wrong (start with `bug:` if "
                               f"the code misses the criteria, `spec:` if the criteria were wrong), then add "
                               f"`{self.cfg['rework_label']}`. A code fix updates this same PR.",
                    "halted": f"Factory run `{run_id}` halted: {detail}\n\nWhat next: if the acceptance criteria were "
                              f"wrong or vague, fix the issue's Acceptance criteria section; if the ticket is too big, "
                              f"split it. Then re-add `{self.cfg['trigger_label']}`: the next run is told why this "
                              f"one stopped. Or take it over by hand, or close the issue.",
                    "rejected": f"Not taken by the factory: {detail}. Edit the issue and re-add "
                                f"`{self.cfg['trigger_label']}` to retry.",
                    "parked": "Paused: the Claude plan limit was reached. The run resumes automatically.",
                }.get(status)
                if msg:
                    self.gh.comment(repo, n, msg[:6000])
        except (httpx.HTTPError, psycopg.Error) as e:
            print(f"[github] report {repo}#{n} {status} failed: {e}", flush=True)


def intake_from_env(dsn: str, inbox: pathlib.Path) -> Intake | None:
    from factory.config import secret
    cfg = config()
    token = secret("github_token")
    if cfg is None or not token:
        return None
    return Intake(GitHub(token), cfg, dsn, inbox)
