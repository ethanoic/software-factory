"""Sandbox broker: the only service that can start containers (spec §1b.3).

The runner is sealed. It shares no folder with the factory or the host:

  1. The factory sends the patched source and the frozen tests as tar archives (no .git, regular files
     only). The broker checks and rebuilds both archives, so nothing it was sent reaches Docker as-is.
  2. Three per-run Docker volumes are made: source (/workspace, writable), tests (/workspace/tests,
     read-only to the runner) and results (/workspace/.factory). A never-started loader container
     copies the archives in through the Docker archive API.
  3. The runner starts from a fixed template: no network, read-only root, no capabilities, UID 10001.
  4. When it exits, the broker copies the results folder out and keeps only known result files that
     are regular files under a size limit. Symlinks, devices and anything else are dropped here, so
     the factory only ever receives JSON. Then the containers and volumes are deleted.

The factory cannot choose the image, command, mounts, network or privileges. The broker talks to Docker
through a socket proxy that allows only the container and volume calls this file makes.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import time

import docker
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from requests.exceptions import ConnectionError as ReqConnErr
from requests.exceptions import ReadTimeout

from archive import MAX_ARCHIVE, Rejected, changed_files_archive, read_results, rebuild_archive

IMAGES = {"python": os.environ.get("RUNNER_IMAGE_PY", "factory-runner-py:locked"),
          "javascript": os.environ.get("RUNNER_IMAGE_TS", "factory-runner-ts:locked"),   # same Node runner
          "typescript": os.environ.get("RUNNER_IMAGE_TS", "factory-runner-ts:locked")}
RUN_ID = re.compile(r"^[a-z0-9-]{8,64}$")
# Per-project runner images built by `make project-rebuild`. Only this shape is accepted, so the factory
# cannot ask for any other image on the machine.
PROJECT_IMAGE = re.compile(r"^factory-runner-[a-z0-9][a-z0-9-]{0,62}:[a-f0-9]{12}$")
WALL_S = int(os.environ.get("RUNNER_WALL_S", "300"))

app = FastAPI()
_client = None


def client():
    global _client
    if _client is None:
        _client = docker.from_env()       # DOCKER_HOST=tcp://docker-proxy:2375 in compose
    return _client


class RunReq(BaseModel):
    run_id: str
    language: str
    source_tar_b64: str = Field(max_length=MAX_ARCHIVE * 4 // 3 + 8)
    tests_tar_b64: str = Field(max_length=MAX_ARCHIVE * 4 // 3 + 8)
    changed_files: list[str] = Field(default_factory=list, max_length=2000)
    image: str | None = None          # a project's runner image; None = the language's default image


# ------------------------------------------------------------------ Docker
def _names(run_id: str) -> tuple[str, str, dict[str, str]]:
    vols = {k: f"factory-{run_id}-{k}" for k in ("src", "tests", "out")}
    return f"runner-{run_id}", f"loader-{run_id}", vols


def _cleanup(run_id: str) -> None:
    c = client()
    runner, loader, vols = _names(run_id)
    for name in (runner, loader):
        try:
            c.containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass
    for v in vols.values():
        try:
            c.volumes.get(v).remove(force=True)
        except docker.errors.NotFound:
            pass


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/run")
def run(req: RunReq):
    if not RUN_ID.fullmatch(req.run_id) or req.language not in IMAGES:
        raise HTTPException(400, "rejected")
    if req.image is not None and not PROJECT_IMAGE.fullmatch(req.image):
        raise HTTPException(400, "rejected: not a factory runner image")
    try:
        source = rebuild_archive(base64.b64decode(req.source_tar_b64, validate=True),
                                 skip_prefixes=("tests/", ".factory/"))   # those are their own volumes
        tests = rebuild_archive(base64.b64decode(req.tests_tar_b64, validate=True))
        changed = changed_files_archive(req.changed_files)
    except (Rejected, binascii.Error) as e:
        raise HTTPException(400, f"rejected: {e}")

    c, image = client(), req.image or IMAGES[req.language]
    runner_name, loader_name, vols = _names(req.run_id)
    _cleanup(req.run_id)                                     # leftovers from a crashed attempt
    labels = {"factory.run_id": req.run_id}
    for v in vols.values():
        c.volumes.create(v, labels=labels)
    rw = {vols["src"]: {"bind": "/workspace", "mode": "rw"},
          vols["tests"]: {"bind": "/workspace/tests", "mode": "rw"},
          vols["out"]: {"bind": "/workspace/.factory", "mode": "rw"}}
    started = time.time()
    try:
        loader = c.containers.create(image, command=["true"], name=loader_name, network_mode="none",
                                     user="10001:10001", volumes=rw, labels=labels)
        for path, data in (("/workspace", source), ("/workspace/tests", tests), ("/workspace/.factory", changed)):
            if not loader.put_archive(path, data):
                raise HTTPException(500, f"could not load {path}")
        loader.remove(force=True)

        r = c.containers.run(
            image,
            command=["/opt/factory/entrypoint.sh"],          # fixed; never from the request
            detach=True,
            name=runner_name,
            network_mode="none",
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=256m"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            pids_limit=256,
            mem_limit="2g",
            memswap_limit="2g",
            nano_cpus=2_000_000_000,
            user="10001:10001",
            working_dir="/workspace",
            environment={"HOME": "/tmp", "CI": "1"},
            volumes={**rw, vols["tests"]: {"bind": "/workspace/tests", "mode": "ro"}},
            labels=labels,
        )
        timed_out = False
        try:
            exit_code = r.wait(timeout=WALL_S)["StatusCode"]
        except (ReadTimeout, ReqConnErr):
            timed_out, exit_code = True, 137
            r.kill()
        logs = r.logs(tail=400).decode(errors="replace")
        try:
            stream, _ = r.get_archive("/workspace/.factory")
            files = read_results(stream)
        except docker.errors.NotFound:
            files = {}
        return {"exit_code": exit_code, "timed_out": timed_out, "log_tail": logs[-8000:],
                "duration_s": round(time.time() - started, 2), "files": files}
    finally:
        _cleanup(req.run_id)
