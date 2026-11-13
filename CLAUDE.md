# Working on the AI Software Factory

A local service on one Mac (one operator, many projects) that turns labelled GitHub issues into draft PRs:
plan → criteria approved by a person → tests by a separate model → tests must fail first → code → sealed
sandbox → QA → independent review → repair (≤ 3) → human approval → draft PR.

- **Spec (decisions, invariants, maintainer guide §7):** https://claude.ai/artifact/DkiySGK2a4xoLeyak7tewW
- **Security review:** https://claude.ai/artifact/9smt2RTH3GVVdCnRpEpoU3
- **Operator docs:** `README.md`; first run: `docs/hello-world-walkthrough.md`.
- The code on `main` is the source of truth. If the spec and the code disagree, the code wins: fix the spec.

## Invariants: do not break these

1. Nothing the sandbox runner can write is executed, followed or trusted by the orchestrator. The runner
   gets archives (`patches.source_archive`, broker `archive.rebuild_archive`), never mounts; results come
   back as JSON and are written by the factory (`nodes.write_results`).
2. Only `docker-proxy` holds the Docker socket. The broker accepts only the language default image or
   `factory-runner-<slug>:<12 hex>`.
3. Every git call the factory makes goes through `patches.git` / `patches.GIT_HARDEN` (no hooks, no fsmonitor).
4. Models get no tools and one turn. The LangGraph graph (`graph.py`) decides every step; no model routes.
5. No model grades its own work: tests come from `write_tests` (signatures only), `red_check` needs no model,
   `review_code` can block but never approve, and `review.py` (not the model) decides what blocks.
6. A human approves before every push. PRs are drafts on `factory/*`. The factory never works on its own repo.
7. Only registered projects are touched; the project record wins over the ticket.
8. Text bound for a prompt, trace, comment or PR goes through `config.redact`.
9. A person owns the acceptance criteria. Criteria written in the issue (`criteria.py`) are used verbatim;
   criteria the planner proposed stop the run at `criteria_gate` until a person approves them
   (`runner.approve_criteria`). No node sets `criteria_approved`, and `make resume` cannot pass the gate.

## Commands

```bash
make test                                   # unit tests; set FACTORY_TEST_DSN=postgresql://… to include Postgres ones
make test-sandbox                           # live sealed-runner test (needs Docker + pip install docker fastapi httpx)
cd orchestrator && POOL_FILE=../pool.yaml LANGFUSE_DISABLED=1 python3 -m pytest -q ../tests   # what make test runs
```

## How changes are made

- One feature branch and one PR per change, to `main` (protected; never push to `main`).
- Update tests, `README.md`, the walkthrough and the spec (§ status table, §7) in the same change.
- New pipeline step: `nodes.py` + `graph.build` + router, `board.py` `STATIONS`, `board_static/app.js`
  `DOING`, stubs in `tests/test_graph_flow.py` and the parking test in `tests/test_postgres.py`.
- New model role: `pool.yaml` `roles`, `pool.SUITE_FOR`, `gateway.TaskKind` + `route`,
  `runner.missing_roles`, and a qualification suite.
- Schema change: a new numbered, idempotent file in `orchestrator/migrations/`.
- Security-relevant changes get a regression test in `tests/test_security.py`.
