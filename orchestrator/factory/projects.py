"""Projects: the repos this factory works on, one row each in the factory's own database.

A project is ready to take tickets when it is active and has a runner image built from its current
dependency files. Tickets for repos that are not registered are refused.

  python -m factory.projects list
  python -m factory.projects add <owner/name> --language node [--branch main] [--max-runs-per-day 2]
  python -m factory.projects pause|resume|remove <owner/name>
  python -m factory.projects limit <owner/name> <n|none>
  python -m factory.projects labelers <owner/name> [login ...]
  python -m factory.projects deps <owner/name>              (tar of its dependency files on stdout; for make)
  python -m factory.projects set-image <owner/name> <image> <deps_hash>   (called by make project-rebuild)
"""
from __future__ import annotations

import argparse
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass

import psycopg

from factory import depshash, patches
from factory.config import is_factory_repo

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]{1,100}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
LANGUAGES = depshash.LANGUAGES
IMAGE_RE = re.compile(r"^factory-runner-[a-z0-9][a-z0-9-]{0,62}:[a-f0-9]{12}$")
COLS = ("repo", "base_branch", "language", "status", "labelers", "max_runs_per_day", "runner_image",
        "deps_hash", "image_built_at")


class ProjectError(Exception):
    pass


@dataclass
class Project:
    repo: str
    base_branch: str
    language: str
    status: str
    labelers: list
    max_runs_per_day: int | None
    runner_image: str | None
    deps_hash: str | None
    image_built_at: object = None

    @property
    def ready(self) -> bool:
        return self.status == "active" and bool(self.runner_image)

    def as_intake(self) -> dict:
        return {"repo": self.repo, "base_branch": self.base_branch, "language": self.language,
                "labelers": list(self.labelers or [])}


def norm_repo(repo: str) -> str:
    repo = (repo or "").strip().lower()
    if not REPO_RE.fullmatch(repo):
        raise ProjectError(f"invalid repo {repo!r}: expected owner/name")
    return repo


def slug(repo: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")[:62] or "project"


def image_name(repo: str, deps_hash: str) -> str:
    return f"factory-runner-{slug(repo.lower())}:{deps_hash[:12]}"


# ------------------------------------------------------------------ storage
def _row(r) -> Project:
    return Project(*r)


def get(dsn: str, repo: str) -> Project | None:
    with psycopg.connect(dsn) as c:
        r = c.execute(f"SELECT {', '.join(COLS)} FROM projects WHERE repo = %s", (repo.lower(),)).fetchone()
    return _row(r) if r else None


def list_all(dsn: str) -> list[Project]:
    with psycopg.connect(dsn) as c:
        return [_row(r) for r in c.execute(f"SELECT {', '.join(COLS)} FROM projects ORDER BY repo").fetchall()]


def ready(dsn: str) -> list[Project]:
    return [p for p in list_all(dsn) if p.ready]


def add(dsn: str, repo: str, language: str, base_branch: str = "main", max_runs_per_day: int | None = None,
        labelers: list[str] | None = None) -> Project:
    repo = norm_repo(repo)
    if is_factory_repo(repo):
        raise ProjectError(f"{repo} is the factory's own repo; the factory never works on itself")
    if language not in LANGUAGES:
        raise ProjectError(f"language must be one of {', '.join(LANGUAGES)}")
    if not BRANCH_RE.fullmatch(base_branch):
        raise ProjectError(f"invalid branch {base_branch!r}")
    for login in labelers or []:
        if not LOGIN_RE.fullmatch(login):
            raise ProjectError(f"invalid GitHub login {login!r}")
    with psycopg.connect(dsn, autocommit=True) as c:
        r = c.execute("""INSERT INTO projects (repo, base_branch, language, max_runs_per_day, labelers)
                         VALUES (%s, %s, %s, %s, %s) ON CONFLICT (repo) DO NOTHING RETURNING repo""",
                      (repo, base_branch, language, max_runs_per_day, json.dumps(labelers or []))).fetchone()
    if r is None:
        raise ProjectError(f"{repo} is already a project; use pause, resume, limit or labelers to change it")
    return get(dsn, repo)


def _update(dsn: str, repo: str, sql: str, args: tuple) -> Project:
    repo = norm_repo(repo)
    with psycopg.connect(dsn, autocommit=True) as c:
        r = c.execute(f"UPDATE projects SET {sql}, updated_at = now() WHERE repo = %s RETURNING repo",
                      (*args, repo)).fetchone()
    if r is None:
        raise ProjectError(f"{repo} is not a project (see: make project-list)")
    return get(dsn, repo)


def set_status(dsn: str, repo: str, status: str) -> Project:
    return _update(dsn, repo, "status = %s", (status,))


def set_limit(dsn: str, repo: str, n: int | None) -> Project:
    if n is not None and n < 0:
        raise ProjectError("the limit must be 0 or more (or none)")
    return _update(dsn, repo, "max_runs_per_day = %s", (n,))


def set_labelers(dsn: str, repo: str, logins: list[str]) -> Project:
    for login in logins:
        if not LOGIN_RE.fullmatch(login):
            raise ProjectError(f"invalid GitHub login {login!r}")
    return _update(dsn, repo, "labelers = %s", (json.dumps(logins),))


def set_image(dsn: str, repo: str, image: str, deps_hash_: str) -> Project:
    if not IMAGE_RE.fullmatch(image) or not re.fullmatch(r"[a-f0-9]{64}", deps_hash_) \
            or image != image_name(repo.lower(), deps_hash_):
        raise ProjectError(f"image {image!r} does not match the project and dependency hash")
    return _update(dsn, repo, "runner_image = %s, deps_hash = %s, image_built_at = now()", (image, deps_hash_))


def remove(dsn: str, repo: str) -> None:
    repo = norm_repo(repo)
    with psycopg.connect(dsn, autocommit=True) as c:
        if c.execute("DELETE FROM projects WHERE repo = %s RETURNING repo", (repo,)).fetchone() is None:
            raise ProjectError(f"{repo} is not a project")


def import_from_yaml(dsn: str, cfg: dict | None) -> list[str]:
    """One-time move: repos still listed in github.yaml become projects (an image is still needed)."""
    added = []
    for r in (cfg or {}).get("repos") or []:
        try:
            add(dsn, r["repo"], r.get("language", "python"), r.get("base_branch", "main"))
            added.append(r["repo"].lower())
        except ProjectError:
            continue                                # already a project, or invalid: leave it alone
    return added


# ------------------------------------------------------------------ dependency files for the runner image
def deps_archive(repo: str, project: Project, runs_root: pathlib.Path, auth_env: dict) -> bytes:
    """Clone the project's base branch (hardened git, into the private runs volume) and return a tar of
    its dependency files plus a `.deps-hash` file. `make project-rebuild` builds the image from it."""
    work = runs_root / "_deps" / slug(repo)
    if work.exists():
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["git", *patches.GIT_HARDEN, "clone", "--depth", "1", "--branch", project.base_branch,
                        f"https://github.com/{repo}.git", str(work)], capture_output=True,
                       env=patches.git_env(auth_env))
    if r.returncode != 0:
        raise ProjectError("clone failed: " + r.stderr.decode(errors="replace")[-400:])
    try:
        h = depshash.deps_hash(work, project.language)
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w") as t:
            for name in depshash.deps_files(work, project.language):
                t.add(work / name, arcname=name, recursive=False)
            for meta, value in ((".deps-hash", h), (".language", project.language),
                                (".image", image_name(repo, h))):
                data = value.encode()
                info = tarfile.TarInfo(meta)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        return out.getvalue()
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ CLI
def _print(projects: list[Project]) -> None:
    if not projects:
        print("No projects yet. Add one: make project-add REPO=owner/name LANGUAGE=node")
        return
    print(f"{'PROJECT':<40} {'STATE':<10} {'LANGUAGE':<11} {'BRANCH':<12} {'RUNS/DAY':<9} RUNNER IMAGE")
    for p in projects:
        state = p.status if p.runner_image else f"{p.status}*"
        cap = "-" if p.max_runs_per_day is None else str(p.max_runs_per_day)
        print(f"{p.repo:<40} {state:<10} {p.language:<11} {p.base_branch:<12} {cap:<9} "
              f"{p.runner_image or 'not built: make project-rebuild REPO=' + p.repo}")
    if any(not p.runner_image for p in projects):
        print("* not ready: no runner image yet")


def main(argv: list[str] | None = None) -> None:
    from factory.deps import Deps
    from factory.runner import migrate
    ap = argparse.ArgumentParser(prog="factory.projects")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("names")
    a_add = sub.add_parser("add")
    a_add.add_argument("repo")
    a_add.add_argument("--language", required=True, choices=LANGUAGES)
    a_add.add_argument("--branch", default="main")
    a_add.add_argument("--max-runs-per-day", type=int)
    for name in ("pause", "resume", "remove", "deps", "show"):
        sub.add_parser(name).add_argument("repo")
    a_lim = sub.add_parser("limit")
    a_lim.add_argument("repo")
    a_lim.add_argument("n")
    a_lab = sub.add_parser("labelers")
    a_lab.add_argument("repo")
    a_lab.add_argument("logins", nargs="*")
    a_img = sub.add_parser("set-image")
    a_img.add_argument("repo")
    a_img.add_argument("image")
    a_img.add_argument("deps_hash")
    a = ap.parse_args(argv)

    deps = Deps.from_env()
    migrate(deps.dsn)
    try:
        if a.cmd == "list":
            return _print(list_all(deps.dsn))
        if a.cmd == "names":
            return print("\n".join(p.repo for p in list_all(deps.dsn)))
        if a.cmd == "add":
            p = add(deps.dsn, a.repo, a.language, a.branch, a.max_runs_per_day)
            return print(f"added {p.repo} ({p.language}, {p.base_branch}). Next: its runner image is built "
                         f"by make project-add, or run make project-rebuild REPO={p.repo}")
        if a.cmd == "show":
            p = get(deps.dsn, a.repo)
            if p is None:
                raise ProjectError(f"{a.repo} is not a project")
            return _print([p])
        if a.cmd in ("pause", "resume"):
            p = set_status(deps.dsn, a.repo, "paused" if a.cmd == "pause" else "active")
            return print(f"{p.repo} is now {p.status}")
        if a.cmd == "remove":
            remove(deps.dsn, a.repo)
            return print(f"removed {a.repo.lower()}. Its past runs stay on the board; its image stays in Docker "
                         f"(docker image rm to delete it).")
        if a.cmd == "limit":
            n = None if a.n.lower() in ("none", "-") else int(a.n)
            p = set_limit(deps.dsn, a.repo, n)
            return print(f"{p.repo}: {'no own limit' if n is None else f'at most {n} runs a day'}")
        if a.cmd == "labelers":
            p = set_labelers(deps.dsn, a.repo, a.logins)
            return print(f"{p.repo}: extra labelers {', '.join(p.labelers) or 'none'}")
        if a.cmd == "deps":
            p = get(deps.dsn, a.repo)
            if p is None:
                raise ProjectError(f"{a.repo} is not a project")
            sys.stdout.buffer.write(deps_archive(p.repo, p, deps.runs_root, deps.git_auth_env()))
            return
        if a.cmd == "set-image":
            p = set_image(deps.dsn, a.repo, a.image, a.deps_hash)
            return print(f"{p.repo} runner image: {p.runner_image}")
    except (ProjectError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
