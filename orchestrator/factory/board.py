"""The factory board: where every ticket is, and what you need to do next.

  python -m factory.board serve [--port 8090]

Read-only by design. It never approves, retries or changes anything: approval stays on the GitHub label
(which checks who applied it) or the command line. A button on a local page cannot tell who clicked it,
and any other site open in your browser could submit to it. The board tells you which label to add or
which command to run instead.

It reads Postgres through a read-only role (BOARD_DB_DSN) and the inbox folder (mounted read-only).
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import pathlib
import re
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

STATIC = pathlib.Path(__file__).resolve().parent / "board_static"
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("text/javascript", ".js")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DOCS = os.environ.get("FACTORY_DOCS_URL", "https://github.com/ethanoic/software-factory/blob/main")
HEARTBEAT_STALE_S = 180
PARK_RETRY_S, PARK_MAX = 60 * 60, 6

# The line, in order. Each station lists the pipeline steps (FSM nodes) shown at it.
STATIONS = [
    ("queued", "Queued", ()),
    ("triage", "Triage", ("ingest", "triage")),
    ("plan", "Plan", ("plan",)),
    ("tests", "Tests", ("freeze_tests",)),
    ("code", "Code", ("generate",)),
    ("sandbox", "Sandbox", ("sandbox",)),
    ("qa", "Checks", ("qa",)),
    ("repair", "Repair", ("repair",)),
    ("approval", "Your approval", ()),
    ("review", "PR review", ()),
]
NODE_STATION = {node: sid for sid, _, nodes in STATIONS for node in nodes}


# ---------------------------------------------------------------------------------------------- links
def issue_url(repo: str, number: int | None) -> str | None:
    if number is None or not REPO_RE.fullmatch(repo or ""):
        return None
    return f"https://github.com/{repo}/issues/{int(number)}"


def safe_github_url(url: str | None) -> str | None:
    """PR links come from GitHub's API; only ever link to github.com."""
    return url if url and re.fullmatch(r"https://github\.com/[A-Za-z0-9_./#?=:-]+", url) else None


def trace_link(run_id: str) -> str | None:
    try:
        from factory.obs import trace_url
        return trace_url(run_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------- actions
def step(text: str, *, url: str | None = None, url_label: str | None = None, command: str | None = None) -> dict:
    return {k: v for k, v in {"text": text, "url": url, "url_label": url_label, "command": command}.items() if v}


def retry_step(card: dict) -> dict:
    if card["issue_url"]:
        return step("Re-add the factory:ready label to run it again.",
                    url=card["issue_url"], url_label=f"Open issue #{card['number']}")
    return step("Drop the ticket file into workspace/inbox again to rerun it.")


# (pattern in the halt reason, why it stopped, steps to fix it before retrying)
HALT_RULES = [
    (r"no qualified model", "No model is approved for one of the steps.",
     [step("Approve a model for the missing step, then retry.",
           command='make pool ARGS="trust <model id from: make pool ARGS=catalog>"')]),
    (r"run cap|daily cap", "It hit the spending cap for paid model calls.",
     [step("Check what it spent in the trace. Then simplify the ticket, or raise RUN_CAP_USD / DAY_CAP_USD in compose.factory.yml.")]),
    (r"repair limit", "Three repair attempts did not get the tests passing.",
     [step("Make the acceptance criteria more concrete (input and expected output), or split the ticket.")]),
    (r"wall clock", "The run took longer than 45 minutes.",
     [step("Split the ticket into smaller changes.")]),
    (r"sandbox environment", "The test runner is missing a tool or dependency, or found no tests.",
     [step("Rebuild the runner image with the target repo's dependencies.",
           command="make runner-ts DEPS=<path to repo>   # or make runner-py")]),
    (r"sandbox tamper", "The frozen acceptance tests changed during the run.",
     [step("Look at the trace before retrying: something wrote to the tests the factory is not allowed to change.")]),
    (r"plan invalid", "The planning step returned something unusable.",
     [step("Retry once. If it repeats, simplify the ticket.")]),
    (r"clone failed", "The factory could not clone the repository.",
     [step("Check the repo name in github.yaml and that the token in secrets/github_token can read it.")]),
    (r"git \w+ failed|PR creation failed", "Pushing the branch or opening the PR failed.",
     [step("Check the GitHub token has Contents and Pull requests write access to this repo.")]),
    (r"context too small|local backend unavailable", "No model could take this ticket's size, or the local model was down.",
     [step("Check the local model server (omlx) is running, or split the ticket.")]),
]


def halt_help(detail: str) -> tuple[str, list[dict]]:
    for pattern, why, steps in HALT_RULES:
        if re.search(pattern, detail or "", re.I):
            return why, steps
    return "It stopped for a reason the board doesn't recognise.", [step("Read the reason and the trace.")]


def card_action(card: dict) -> dict | None:
    status, ref = card["status"], card["ref"]
    if status == "awaiting_approval":
        repairs = card["repair_attempts"]
        why = ("The new tests and the security scan passed"
               + (f" after {repairs} repair{'s' if repairs != 1 else ''}." if repairs else " on the first try."))
        steps = [step("Read what it did in the trace if you want to check first.", url=card["trace_url"],
                      url_label="Open trace")] if card["trace_url"] else []
        if card["issue_url"]:
            steps.append(step("Add the factory:approve label to open the draft PR.",
                              url=card["issue_url"], url_label=f"Open issue #{card['number']}"))
            steps.append(step("Or approve from the terminal:", command=f"make approve RUN={card['id']}"))
        else:
            steps.append(step("Approve from the terminal to open the draft PR:", command=f"make approve RUN={card['id']}"))
        return {"tone": "yours", "rank": 3, "title": f"Approve {ref}", "why": why, "steps": steps}
    if status == "pr_open":
        steps = [step("Review the change, mark the PR ready for review, then merge it.",
                      url=card["pr_url"], url_label="Open PR")] if card["pr_url"] else \
                [step("Review and merge the PR the factory opened.")]
        if card["issue_url"]:
            steps.append(step(f"Merging closes issue #{card['number']}, and the board moves it to finished."))
        return {"tone": "yours", "rank": 4, "title": f"Review and merge the PR for {ref}",
                "why": "The draft PR is open and waiting for you.", "steps": steps}
    if status == "halted":
        why, steps = halt_help(card["detail"])
        steps = [*steps, retry_step(card)]
        if card["trace_url"]:
            steps.insert(0, step("See exactly where it stopped.", url=card["trace_url"], url_label="Open trace"))
        return {"tone": "stopped", "rank": 2, "title": f"Fix and retry {ref}", "why": why,
                "reason": card["detail"], "steps": steps}
    return None


def system_actions(health: dict | None, health_age: float | None, docs: str = DOCS) -> list[dict]:
    out = []
    if health is None or health_age is None or health_age > HEARTBEAT_STALE_S:
        seen = "has never reported in" if health is None else f"last reported {int((health_age or 0) // 60)} minutes ago"
        out.append({"tone": "stopped", "rank": 0, "title": "Start the factory",
                    "why": f"The factory {seen}, so nothing is being picked up.",
                    "steps": [step("Start it:", command="make up"), step("If it stops again, see why:", command="make logs")]})
        return out
    if health.get("missing_roles"):
        roles = ", ".join(health["missing_roles"])
        out.append({"tone": "stopped", "rank": 1, "title": "Approve a model",
                    "why": f"No model is approved for: {roles}. Every run will stop at that step.",
                    "steps": [step("List the models the factory found:", command='make pool ARGS="refresh"'),
                              step("Approve one:", command='make pool ARGS="trust <model id>"'),
                              step("How this works", url=f"{docs}/docs/hello-world-walkthrough.md#5-start-the-factory-and-approve-a-model",
                                   url_label="Walkthrough, step 5")]})
    if health.get("token_warning"):
        expired = "expired" in health["token_warning"] or "No Claude" in health["token_warning"]
        out.append({"tone": "stopped" if expired else "yours", "rank": 1 if expired else 5,
                    "title": "Renew the Claude token", "why": health["token_warning"].split(". On the Mac")[0] + ".",
                    "steps": [step("On the Mac, in the factory folder:", command="make claude-token")]})
    return out


# ---------------------------------------------------------------------------------------------- board
def _iso(dt) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def queued_cards(inbox: pathlib.Path) -> list[dict]:
    cards = []
    files = sorted(inbox.glob("*.json"), key=lambda p: p.stat().st_mtime) if inbox.is_dir() else []
    for i, f in enumerate(files):
        try:
            t = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        repo, number = str(t.get("repo", "")), t.get("issue_number")
        cards.append({"id": f.name, "ticket_id": str(t.get("ticket_id", f.stem))[:120],
                      "title": str(t.get("title", ""))[:300], "repo": repo, "number": number,
                      "issue_url": issue_url(repo, number), "status": "queued", "station": "queued",
                      "stage": None, "repair_attempts": 0, "detail": None, "pr_url": None, "trace_url": None,
                      "models": {}, "position": i + 1,
                      "updated_at": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()})
    return cards


def parked_meta(inbox: pathlib.Path) -> dict[str, dict]:
    out = {}
    for f in (inbox / "parked").glob("*.json") if (inbox / "parked").is_dir() else []:
        try:
            m = json.loads(f.read_text())
            out[m["run_id"]] = m
        except (OSError, ValueError, KeyError):
            continue
    return out


def build_board(dsn: str, inbox: pathlib.Path, now: float | None = None) -> dict:
    now = now or time.time()
    with psycopg.connect(dsn) as c:
        rows = c.execute("""
            SELECT DISTINCT ON (ticket_id) run_id, ticket_id, title, repo, issue_number, status, stage, detail,
                   repair_attempts, models, pr_url, started_at, updated_at
            FROM runs
            WHERE run_id NOT LIKE 'qual-%%'
              AND (status IN ('running', 'parked', 'awaiting_approval')
                   OR (status IN ('halted', 'pr_open') AND updated_at > now() - interval '14 days')
                   OR (status IN ('done', 'rejected') AND updated_at > now() - interval '7 days'))
            ORDER BY ticket_id, started_at DESC""").fetchall()
        h = c.execute("SELECT data, extract(epoch FROM now() - last_seen) FROM factory_health WHERE id = 1").fetchone()
        unauthorized = c.execute("""SELECT repo, number FROM github_issues WHERE status = 'unauthorized'
                                    AND updated_at > now() - interval '7 days' ORDER BY updated_at DESC""").fetchall()
    health, health_age = (h[0], float(h[1])) if h else (None, None)
    parked = parked_meta(inbox)

    cards = queued_cards(inbox)
    for (run_id, ticket_id, title, repo, number, status, stage, detail, repairs, models, pr_url,
         started, updated) in rows:
        if status in ("running", "parked", "halted"):
            station = NODE_STATION.get(stage or "", "triage")
        elif status == "awaiting_approval":
            station = "approval"
        elif status == "pr_open":
            station = "review"
        else:
            station = "finished"
        card = {"id": run_id, "ticket_id": ticket_id, "title": title, "repo": repo, "number": number,
                "issue_url": issue_url(repo, number), "status": status, "station": station, "stage": stage,
                "repair_attempts": repairs or 0, "detail": detail, "pr_url": safe_github_url(pr_url),
                "trace_url": trace_link(run_id), "models": models or {},
                "started_at": _iso(started), "updated_at": _iso(updated)}
        if status == "parked":
            m = parked.get(run_id, {})
            card["resume_at"] = datetime.fromtimestamp(m["parked_at"] + PARK_RETRY_S, timezone.utc).isoformat() \
                if m.get("parked_at") else None
            card["retries"] = f"{m.get('retries', 0)} of {PARK_MAX}"
        if status in ("halted", "rejected"):
            card["detail"] = (detail or "")[:400]
        cards.append(card)

    # With one repo watched, "#7" is clearer than "owner/repo#7"; the full name returns with several repos.
    repos = {c["repo"] for c in cards} | set((health or {}).get("intake_repos", []))
    for c in cards:
        c["ref"] = (f"#{c['number']}" if len(repos) <= 1 else f"{c['repo']}#{c['number']}") \
            if c["number"] else c["ticket_id"]

    actions = system_actions(health, health_age)
    for card in cards:
        a = card_action(card)
        if a:
            a["card"] = card["id"]
            card["action"] = True
            actions.append(a)
    for repo, n in unauthorized:
        url = issue_url(repo, n)
        actions.append({"tone": "yours", "rank": 6, "title": f"Decide on #{n}" if len(repos) <= 1 else f"Decide on {repo}#{n}",
                        "why": "Someone without write access added factory:ready, so the factory ignored it.",
                        "steps": [step("If you want it run, add the factory:ready label yourself.",
                                       url=url, url_label=f"Open issue #{n}")]})
    actions.sort(key=lambda a: a["rank"])

    limit = (health or {}).get("daily_limit")
    used = (health or {}).get("runs_today", 0)
    return {
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "health": {"online": health is not None and health_age is not None and health_age <= HEARTBEAT_STALE_S,
                   "seconds_since_heartbeat": None if health_age is None else int(health_age),
                   "runs_today": used, "daily_limit": limit,
                   "runs_left": None if limit is None else max(0, limit - used),
                   "repos": (health or {}).get("intake_repos", [])},
        "stations": [{"id": sid, "name": name} for sid, name, _ in STATIONS],
        "actions": actions,
        "cards": [c for c in cards if c["station"] != "finished"],
        "finished": [c for c in cards if c["station"] == "finished"],
    }


# ---------------------------------------------------------------------------------------------- server
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
                               "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def make_handler(dsn: str, inbox: pathlib.Path, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "factory-board"

        def log_message(self, *_):                    # quiet: the board is polled every few seconds
            pass

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            # DNS-rebinding guard: only answer requests addressed to this machine by a known name.
            if (self.headers.get("Host") or "").lower() not in allowed_hosts:
                return self._send(421, b"Misdirected request", "text/plain; charset=utf-8")
            path = self.path.split("?", 1)[0]
            if path == "/api/board":
                try:
                    data = build_board(dsn, inbox)
                except psycopg.Error as e:
                    return self._send(503, json.dumps({"error": f"database unavailable: {e.__class__.__name__}"}).encode(),
                                      "application/json")
                return self._send(200, json.dumps(data).encode(), "application/json")
            name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            f = (STATIC / name).resolve()
            if not f.is_file() or STATIC.resolve() not in f.parents:
                return self._send(404, b"Not found", "text/plain; charset=utf-8")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            cache = "public, max-age=86400" if name.startswith("fonts/") else "no-cache"
            return self._send(200, f.read_bytes(), ctype, cache)

        def _not_allowed(self):
            self._send(405, b"The board is read-only", "text/plain; charset=utf-8")

        do_POST = do_PUT = do_PATCH = do_DELETE = _not_allowed  # noqa: N815

    return Handler


def serve(port: int, bind: str) -> None:
    dsn = os.environ.get("BOARD_DB_DSN") or os.environ["FACTORY_DB_DSN"]
    inbox = pathlib.Path(os.environ.get("WORKSPACE_ROOT", "/workspace")) / "inbox"
    public_port = os.environ.get("BOARD_PUBLIC_PORT", str(port))
    hosts = {h.strip().lower() for h in os.environ.get(
        "BOARD_ALLOWED_HOSTS", f"localhost:{public_port},127.0.0.1:{public_port}").split(",") if h.strip()}
    httpd = ThreadingHTTPServer((bind, port), make_handler(dsn, inbox, hosts))
    print(f"[board] serving on {bind}:{port}; open http://localhost:{public_port}", flush=True)
    httpd.serve_forever()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="board")
    ap.add_argument("cmd", choices=["serve"])
    ap.add_argument("--port", type=int, default=int(os.environ.get("BOARD_PORT", "8090")))
    ap.add_argument("--bind", default=os.environ.get("BOARD_BIND", "0.0.0.0"))   # in-container; publish to 127.0.0.1 only
    a = ap.parse_args(argv)
    serve(a.port, a.bind)


if __name__ == "__main__":
    main()
