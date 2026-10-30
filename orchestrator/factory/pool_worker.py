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
    from factory.pool import suite_sha
    with psycopg.connect(deps.dsn) as c:
        rows = c.execute("""SELECT q.role, q.model_id, q.status, q.pass_at_1, q.usd_per_task, q.qualified_at,
                                   q.manual, q.suite_sha256
                            FROM model_qualification q ORDER BY q.role, q.status DESC, q.pass_at_1 DESC""").fetchall()
    if not rows:
        print("no qualified or trusted models yet: run `once` to qualify, or `trust <model_id>` (see README)")
    for role, model, status, score, usd, at, manual, sha in rows:
        how = "trusted (not measured)" if manual else f"pass@1 {float(score):.2f}  ${float(usd):.4f}/task"
        stale = "" if role not in policy()["roles"] or sha == suite_sha(role) else "  [suite changed: inactive]"
        print(f"{role:<16} {model:<48} {status:<10} {how}  {at:%Y-%m-%d}{stale}")


def show_catalog() -> None:
    rows = get_pool().catalog()
    if not rows:
        print("catalog is empty: run `pool_worker refresh` (needs network for the remote catalog)")
    for model, tier, route, pin, pout, ctx in rows:
        price = "local, $0" if tier == "t2" else f"${float(pin):.2f} / ${float(pout):.2f} per 1M"
        print(f"{model:<48} {tier}  route={route:<24} {price:<26} ctx={ctx}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="pool_worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="refresh + qualify on the pool.yaml schedule")
    sub.add_parser("once", help="one refresh + qualify cycle now")
    sub.add_parser("refresh", help="discover models only (no qualification, no model calls)")
    sub.add_parser("catalog", help="list discovered models")
    sub.add_parser("list", help="list qualified, trusted and promoted models per role")
    p = sub.add_parser("promote", help="approve a qualified challenger for a role")
    p.add_argument("model_id"); p.add_argument("role")
    t = sub.add_parser("trust", help="approve a model WITHOUT measuring it (for getting started)")
    t.add_argument("model_id"); t.add_argument("roles", nargs="*", help="default: every role the model can fill")
    a = ap.parse_args(argv)
    deps = Deps.from_env()
    migrate(deps.dsn)
    if a.cmd == "serve":
        serve(deps)
    elif a.cmd == "once":
        cycle(deps)
    elif a.cmd == "refresh":
        print(f"catalog refreshed: {get_pool().refresh()} eligible models")
        show_catalog()
    elif a.cmd == "catalog":
        show_catalog()
    elif a.cmd == "list":
        show(deps)
    elif a.cmd == "trust":
        try:
            roles = get_pool().trust(a.model_id, a.roles or None)
        except ValueError as e:
            raise SystemExit(str(e))
        print(f"trusted {a.model_id} for: {', '.join(roles)} (manual; replaced by the first real measurement)")
    else:
        if a.role not in policy()["roles"]:
            raise SystemExit(f"unknown role {a.role}")
        if not get_pool().promote(a.model_id, a.role):
            raise SystemExit(f"{a.model_id} has no qualification for {a.role} on the current suite; "
                             "qualify it first, or use `trust`")
        print(f"promoted {a.model_id} for {a.role}")


if __name__ == "__main__":
    main()
