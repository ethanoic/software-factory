"""Sandbox broker: the ONLY process with the Docker socket (spec §1b.3).

Accepts {run_id, language} and starts a runner from a fixed template. The orchestrator cannot choose
the image, command, mounts, network or privileges, so a prompt-injected orchestrator still cannot
ask for a privileged container or an extra host mount.
"""
import os
import re
import time

import docker
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from requests.exceptions import ConnectionError as ReqConnErr
from requests.exceptions import ReadTimeout

HOST_ROOT = os.environ["HOST_WORKSPACE_ROOT"]          # absolute macOS path of ./workspace
IMAGES = {"python": os.environ.get("RUNNER_IMAGE_PY", "factory-runner-py:locked"),
          "javascript": os.environ.get("RUNNER_IMAGE_TS", "factory-runner-ts:locked"),   # same Node runner
          "typescript": os.environ.get("RUNNER_IMAGE_TS", "factory-runner-ts:locked")}
RUN_ID = re.compile(r"^[a-z0-9-]{8,64}$")
WALL_S = int(os.environ.get("RUNNER_WALL_S", "300"))

client = docker.from_env()
app = FastAPI()


class RunReq(BaseModel):
    run_id: str
    language: str


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/run")
def run(req: RunReq):
    if not RUN_ID.fullmatch(req.run_id) or req.language not in IMAGES:
        raise HTTPException(400, "rejected")               # no path traversal via run_id
    base = f"{HOST_ROOT}/runs/{req.run_id}"
    name = f"runner-{req.run_id}"
    try:                                                   # stale container from a crashed attempt
        client.containers.get(name).remove(force=True)
    except docker.errors.NotFound:
        pass
    started = time.time()
    c = client.containers.run(
        IMAGES[req.language],
        command=["/opt/factory/entrypoint.sh"],            # fixed; never from the request
        detach=True,
        name=name,
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
        volumes={
            f"{base}/repo": {"bind": "/workspace", "mode": "rw"},
            f"{base}/frozen": {"bind": "/workspace/tests", "mode": "ro"},
        },
        labels={"factory.run_id": req.run_id},
    )
    timed_out = False
    try:
        exit_code = c.wait(timeout=WALL_S)["StatusCode"]
    except (ReadTimeout, ReqConnErr):
        timed_out, exit_code = True, 137
        c.kill()
    logs = c.logs(tail=400).decode(errors="replace")
    c.remove(force=True)
    return {"exit_code": exit_code, "timed_out": timed_out, "log_tail": logs[-8000:],
            "duration_s": round(time.time() - started, 2)}
