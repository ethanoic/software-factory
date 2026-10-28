"""Nightly discovery + qualification worker.

  python -m factory.pool_worker serve                    run refresh + qualify on the pool.yaml cron
  python -m factory.pool_worker once                     run one cycle now
  python -m factory.pool_worker list                     show catalog and qualification state
  python -m factory.pool_worker promote <model> <role>   approve a promotion (human decision)
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import time
import zoneinfo

import psycopg

from factory.config import policy
from factory.deps import Deps
from factory.obs import sync_langfuse_prices
from factory.pool import get_pool
from factory.qualify import qualify_candidates
from factory.runner import migrate


def cycle(deps: Deps) -> None:
    pool = get_pool()
    n = pool.refresh()
    print(f"[pool] catalog refreshed: {n} eligible models", flush=True)
    with psycopg.connect(deps.dsn) as c:
        priced = c.execute("""SELECT model_id, in_per_m, out_per_m FROM model_catalog
                              WHERE tier = 't3' AND NOT retired AND in_per_m IS NOT NULL""").fetchall()
    if os.environ.get("LANGFUSE_DISABLED") != "1":
        try:
            sync_langfuse_prices([(m, float(i), float(o)) for m, i, o in priced])
        except Exception as e:
            print(f"[pool] langfuse price sync failed: {e}", flush=True)
    qualify_candidates(pool, deps)


def next_fire(cron: str, now: dt.datetime) -> dt.datetime:
    from croniter import croniter
    return croniter(cron, now).get_next(dt.datetime)


def serve(deps: Deps) -> None:
    tz = zoneinfo.ZoneInfo(os.environ.get("FACTORY_TZ", "Asia/Singapore"))
    cron = policy()["discovery"]["refresh_cron"]
    while True:
        now = dt.datetime.now(tz)
        fire = next_fire(cron, now)
        print(f"[pool] next cycle at {fire:%Y-%m-%d %H:%M %Z}", flush=True)
        time.sleep(max(1, (fire - now).total_seconds()))
        try:
            cycle(deps)
        except Exception as e:                 # never let one bad night kill the worker
            print(f"[pool] cycle failed: {e!r}", flush=True)


def show(deps: Deps) -> None:
    with psycopg.connect(deps.dsn) as c:
        rows = c.execute("""SELECT q.role, q.model_id, q.status, q.pass_at_1, q.usd_per_task, q.qualified_at
                            FROM model_qualification q ORDER BY q.role, q.status DESC, q.pass_at_1 DESC""").fetchall()
    for r in rows:
        print(f"{r[0]:<16} {r[1]:<48} {r[2]:<10} {float(r[3]):.2f}  ${float(r[4]):.4f}  {r[5]:%Y-%m-%d}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="pool_worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "once", "list"):
        sub.add_parser(name)
    p = sub.add_parser("promote"); p.add_argument("model_id"); p.add_argument("role")
    a = ap.parse_args(argv)
    deps = Deps.from_env()
    migrate(deps.dsn)
    if a.cmd == "serve":
        serve(deps)
    elif a.cmd == "once":
        cycle(deps)
    elif a.cmd == "list":
        show(deps)
    else:
        if a.role not in policy()["roles"]:
            raise SystemExit(f"unknown role {a.role}")
        get_pool().promote(a.model_id, a.role)
        print(f"promoted {a.model_id} for {a.role}")


if __name__ == "__main__":
    main()
