# AI Software Factory (local, M1 Max)

Turns a ticket into a draft pull request: **triage → plan → frozen tests → generate → sandbox → QA →
repair (≤ 3) → human approval → draft PR**. Claude (on your Pro plan) writes every line of code; a
small local model only triages tickets. This repo is the reference implementation of the technical
specification; where a snippet in the spec differs, this code wins.

**New here?** After Setup, follow the [hello-world walkthrough](docs/hello-world-walkthrough.md): it takes
the sample Node.js app in `examples/hello-node` from a GitHub issue to a merged PR.

```text
 GitHub issues labelled factory:ready (polled)  +  workspace/inbox/*.json
        │
        ▼
 ┌──────────────┐  internal   ┌────────────────┐  docker.sock  ┌──────────────────────────┐
 │ orchestrator │────────────▶│ sandbox-broker │──────────────▶│ runner-<run_id>          │
 │ LangGraph FSM│  control net└────────────────┘               │ network=none, read-only, │
 │ (no socket)  │                                              │ UID 10001, 300 s         │
 └──┬────┬──────┘                                              └──────────────────────────┘
    │    │ data net (internal)
    │    ├──────────▶ postgres  (checkpoints, run ledger, model pool)
    │    └──────────▶ langfuse  (traces, cost, savings)
    │ control net
    ▼
 ┌─────────────┐ allowlist: api.anthropic.com · github.com · openrouter.ai (catalog) ·
 │ egress-proxy│            host.docker.internal:8000 (omlx) · optional provider APIs
 └─────────────┘
 pool-worker: nightly 03:17 — discover models, qualify them, request promotions
```

## Final decisions this build implements

| # | Decision |
| --- | --- |
| D1 | LangGraph + Postgres checkpointer; human approval before every PR; PRs open as drafts on `factory/<ticket>` |
| D2 | Coding roles use Anthropic models only; which Claude model is chosen by your qualification suite, never hardcoded |
| D3 | Claude Pro via the Claude Agent SDK, signed in with a plan token from `make claude-token` (Docker secret), tools disabled |
| D4 | `local_mode: assist`: a 4–8B MLX model via omlx triages tickets; 10 GB resident cap |
| D5 | OpenRouter catalog for discovery and list prices only; no code goes through an aggregator; no LiteLLM |
| D6 | Model evals ≤ US$70/month billed (≈ SGD 90); Claude evals on Pro cost $0 but use plan limits |
| D7 | 3 repairs max; US$2/run and US$10/day for billed calls; 4 runs/day on Pro; 45 min per run |
| D8 | Broker-only Docker socket; runner with no network; egress allowlist; hashed lockfiles |
| D9 | GitHub Issues polled every 2 min per repo in `github.yaml`; only issues a maintainer labels `factory:ready`; status written back as labels and comments. JSON files in `workspace/inbox/` stay as a fallback |
| D10 | Self-hosted Langfuse: one trace per run, explicit cost, `savings_usd` at list price |

**Upgrade trigger (D3/D4):** if runs park on Pro limits more than twice a week, or the factory blocks
your own coding, move to Max 5x, claim its US$100/month API credit into a Console organization, set
`anthropic_route: api_key` and `ANTHROPIC_API_KEY`, and empty `secrets/claude_oauth_token`.

## Setup (Phase 0)

Run everything from the folder that holds your existing `compose.yml` (postgres + Langfuse). Copy this
repo's files next to it.

1. **Docker Desktop**: Settings → Resources → Memory 10 GB, swap 1 GB, CPUs 6. File sharing: remove
   `/Users`, add only this folder (rule R1). Turn on Enhanced Container Isolation if your plan has it.
2. `make init` — creates `workspace/`, `secrets/`, `.env.factory` (chmod 600).
3. **Claude Pro token**: `make claude-token` (see "Claude plan token" below). It uses your current
   claude.ai login and stores a one-year token in `secrets/claude_oauth_token`. Keep `ANTHROPIC_API_KEY`
   and `ANTHROPIC_AUTH_TOKEN` unset.
4. **GitHub**: create a fine-grained token limited to the repos in `github.yaml`, with *Contents*,
   *Pull requests* and *Issues*: read/write, and *Metadata*: read. Save it to `secrets/github_token`.
   Protect `main` (rule R9). Then list your repos in `github.yaml` (see "GitHub intake" below).
5. **Langfuse**: create a project, put its keys in `.env.factory`.
6. **Postgres**: set `FACTORY_DB_PASSWORD` in `.env.factory`, then `make db`. If your services are not
   named `postgres` and `langfuse-web`, rename them at the bottom of `compose.factory.yml`.
7. **Local model**: install a 4–8B instruct model with an MLX 4-bit build into your omlx models folder
   (one folder per model, folder name = the alias omlx serves). Bind omlx to `127.0.0.1:8000`.
8. `make hoststat OMLX_MODELS=/path/to/your/mlx/models` — installs the launchd memory sensor.
9. `make lock` (needs [uv](https://docs.astral.sh/uv/)) — writes hashed lockfiles. The images refuse to
   build without them.
10. `make runner-py DEPS=/path/to/target-repo` (or `runner-ts`) — bakes the target repo's dependencies
    into the runner image, because the runner has no network.
11. `make up`, then `make logs`.

## Claude plan token

The factory reaches Claude with a long-lived token tied to your Pro plan, made by Claude Code's own
[`claude setup-token`](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token).

```bash
make claude-token          # opens claude.ai in your browser; click Authorize; the token is captured
make claude-token-check    # shows its expiry, then makes one tiny call from inside the factory
```

- **Uses your current login.** If your browser is already signed in to claude.ai, you only approve
  access. You need Claude Code installed on the Mac (`claude --version`).
- **Separate from your own sessions.** It mints a new token for scripts and leaves your interactive
  Claude Code login alone. The factory deliberately does not copy the login Claude Code keeps in your
  macOS Keychain: that would share and refresh one session between two programs, and it is not a
  documented interface.
- **Stored as a Docker secret.** `secrets/claude_oauth_token` (mode 600), passed only to the Claude Code
  process inside the container, never to the whole container's environment. The script never prints
  it, and blanks any old copy in `.env.factory`.
- **Lasts one year.** The factory alerts daily from 30 days before expiry; renew with
  `make claude-token`. `make claude-token-paste` stores a token you already have.
- **Treat it like a password.** It can make model requests on your plan. Individual use on your own
  machine only.

## Qualify before the first ticket

No model can fill a role until it passes your suite (D2) or you trust it by hand, so the first factory run
halts with "no qualified model" until you do one of the two.

**To get started: trust a model.** `make pool ARGS="refresh"` lists the discovered models; then
`make pool ARGS="trust <model_id>"` approves one for every role it can fill (Claude: all coding roles plus
`spillover`; a local model: `generate` and `classify`). A trusted model is marked *not measured*, is
replaced by its first real measurement, and stops counting when that role's suite files change. The
[walkthrough](docs/hello-world-walkthrough.md#5-start-the-factory-and-approve-a-model) uses this path.

**Before Phase 3: measure.**

1. Add 10–20 solved tickets to `qualification/code/` and 50 labelled tickets to
   `qualification/triage/` (formats in `qualification/README.md`). Delete the two examples.
2. Run one cycle now: `make pool ARGS="once"`
3. Read the alerts (or `make pool ARGS="list"`) and approve the winners:
   `make pool ARGS="promote <model_id> <role>"` for `plan`, `generate_remote`, `repair`,
   `security_fix`, `spillover` and `classify`. An empty suite folder is skipped, never scored.

Code-suite qualification runs on your Pro limits. Run it in the evening, not while you are coding.

## GitHub intake

Configure in `github.yaml`: the repos to watch (`owner/name`, base branch, language), the poll
interval (default 120 s, minimum 60), the two labels, and who may apply them. The factory creates its
labels on each repo the first time it polls.

Idle polls are free: the factory sends the list's ETag and GitHub answers `304 Not Modified`, which
does not count against the API rate limit.

**Only labelled issues are taken, and the labeller is checked.** `factory:ready` and
`factory:approve` count only when applied by someone with write, maintain or admin access, or by a
login in `labelers` (`allowlist_only: true` trusts the list alone). Anything else gets one comment
and is ignored, so strangers filing issues cannot spend your Claude plan.

| You do | The factory does |
| --- | --- |
| Add `factory:ready` to an issue | Removes it, sets `factory:queued`, runs it within the daily limit |
| — | `factory:running` → then one of the outcomes below, each with a short comment |
| — | `factory:rejected` (triage said no) or `factory:halted` (reason given) |
| — | `factory:parked` when the Claude plan limit is hit; resumes on its own |
| — | `factory:awaiting-approval` when tests and QA pass |
| Add `factory:approve` | Opens the draft PR (`Closes #n`), sets `factory:pr-open` |
| Re-add `factory:ready` to a finished issue | Starts a fresh run |

Set `enabled: false` in `github.yaml` to run on the inbox folder only.

## Daily use

- Label issues `factory:ready` on GitHub, or drop a ticket JSON into `workspace/inbox/`
  (format in `workspace/inbox/README.md`).
- The factory alerts when a run **passes** (approve with the `factory:approve` label or
  `make approve RUN=<run_id>`), **halts** (reason + Langfuse trace link), or **parks** on plan limits
  (it retries hourly, up to 6 times).
- `make status` lists recent runs and spend.

## Rollout gates

| Phase | Exit gate |
| --- | --- |
| 0 Correct premises | omlx reachable through the proxy; Langfuse up with Docker capped at 10 GB; no swap during a triage call |
| 1 Skeleton | 10 historical tickets end to end; all acceptance checks below green |
| 2 Observability + breaker | per-node cost visible; forced-loop and $-spike chaos tests trip and alert within one transition |
| 3 Local triage trial | local triage matches your call on ≥ 90% of 50 labelled tickets, else `local_mode: off` |
| 4 Shadow mode | 2 weeks; ≥ 70% of draft PRs merged with ≤ 1 human edit |
| 5 Operational | weekly: cost per merged PR, parks per week (upgrade trigger), breaker trips |

## Acceptance checks (Phase 1)

`make test` runs the unit tests (routing, diff validation, FSM routers, failure classification).
These need the running stack and are checked by hand in Phase 1:

| Check | How |
| --- | --- |
| Runner has no network | a ticket whose test opens a socket to 1.1.1.1 must fail, not hang |
| Runner cannot see home | a test that lists `/Users` must fail |
| Frozen tests are tamper-proof | edit a file in `workspace/runs/<id>/frozen` during a run → run halts as `tamper` |
| Breaker trips before the call | set `RUN_CAP_USD=0.01` with a billed route → halt + alert, no provider call |
| 4th repair refused | a ticket whose tests cannot pass halts after 3 repairs |
| Local cost is $0.00 | Langfuse shows the triage generation at $0.00 with a `savings_usd` score |
| Plan limit parks the run | when Pro limits hit, the run moves to `inbox/parked/` and resumes later |
| Outsider labels are ignored | from an account without write access, add `factory:ready` to a test issue → one comment, no run |

## What has been tested, and what has not

Tested in this build (`make test`, 76 tests; set `FACTORY_TEST_DSN` to include the 20 Postgres ones):
routing for all three `local_mode`s, diff validation and forbidden paths, every FSM router, the real
LangGraph wiring with stubbed nodes (repair loop, 3-repair cap, human gate, tamper halt), the breaker
against real Postgres (run cap, daily cap, persistent trip), pool selection and catalog refresh,
GitHub intake against a fake GitHub API (labeller checks, PR skipping, no double-queueing, free 304
polls, status labels and comments, approval by label, allowlist mode, GitHub outages),
parking on Claude plan limits and resuming from the Postgres checkpoint without re-running finished
steps, and the Agent SDK adapter against the real `ClaudeAgentOptions` of the locked SDK version.
Manual trust, promotion and the `pool_worker` commands were run against Postgres. The runner entrypoint
was run end to end through the real factory steps on a Python sample (wrong patch → repair → green →
dry-run PR → tamper detected) and on `examples/hello-node` with the Node runner's exact tooling (wrong
logic → assertion; unused variable → lint repair; `eval` → blocking security finding; clean fix → dry-run
PR; no ESLint config → lint skipped; Jest missing → halt, not a paid repair). `scripts/claude-token.sh`
was run against a fake `claude` CLI that prints setup-token's real output style, through both the Linux
and a BSD-syntax `script(1)`: capture, masking, file mode, in-place rewrite, paste fallback, failed
sign-in and expiry reporting. A real `claude setup-token` sign-in has not been run.

Not yet exercised (Phase 0–1 on your Mac): Docker builds and the broker's container isolation, the
egress proxy, real Claude/omlx calls, Langfuse ingestion, and the real GitHub API (push, PR creation,
label and permission calls). Check in
particular:
- `host.docker.internal` resolving from the `egress-proxy` container on your Docker Desktop version.
- Langfuse SDK v3 span API and the public models API field names in `factory/obs.py` against your
  Langfuse server version.
- Lockfiles were generated for linux/arm64, Python 3.12 (`make lock` regenerates them).
