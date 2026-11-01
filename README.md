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
 ┌──────────────┐ source + tests ┌────────────────┐  container  ┌──────────────┐ docker.sock
 │ orchestrator │  as archives   │ sandbox-broker │  + volume   │ docker-proxy │────────────▶ Docker
 │ LangGraph FSM│───────────────▶│ (no socket, no │  calls only │ (only holder │
 │ holds tokens,│◀───────────────│  host folders) │────────────▶│  of socket)  │
 │ git clones in│ results as JSON└───────┬────────┘             └──────────────┘
 │ factory-runs │                        │ archive in / results out, then delete
 │ volume       │                        ▼
 └──┬────┬──────┘              ┌──────────────────────────────────────────┐
    │    │                     │ runner-<run_id>: sealed                  │
    │    │                     │ network=none · read-only root · UID 10001│
    │    │                     │ no .git · no host folders · no tokens    │
    │    │                     │ tests read-only · 300 s · then deleted   │
    │    │                     └──────────────────────────────────────────┘
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
| D8 | Sealed runner: no network, no shared folders, no `.git`, no tokens; code goes in and results come out as archives through the broker. Only `docker-proxy` holds the Docker socket. The factory never works on its own repo. Egress allowlist; hashed lockfiles |
| D9 | GitHub Issues polled every 2 min per repo in `github.yaml`; only issues a maintainer labels `factory:ready`; status written back as labels and comments. JSON files in `workspace/inbox/` stay as a fallback |
| D10 | Self-hosted Langfuse: one trace per run, explicit cost, `savings_usd` at list price |

**Upgrade trigger (D3/D4):** if runs park on Pro limits more than twice a week, or the factory blocks
your own coding, move to Max 5x, claim its US$100/month API credit into a Console organization, set
`anthropic_route: api_key` and `ANTHROPIC_API_KEY`, and empty `secrets/claude_oauth_token`.

## Setup (Phase 0)

Run everything from the folder that holds your existing `compose.yml` (postgres + Langfuse). Copy this
repo's files next to it.

1. **Docker Desktop**: Settings → Resources → Memory 10 GB, swap 1 GB, CPUs 6. File sharing: remove
   `/Users`, add only this folder (rule R1). Runners never use file sharing; only the factory's own
   services read this folder (inbox, config, secrets). If you turn on Enhanced Container Isolation,
   allow the `tecnativa/docker-socket-proxy` image to mount the Docker socket, or `docker-proxy` cannot start.
2. `make init` — creates `workspace/`, `secrets/`, `.env.factory` (chmod 600).
3. **Claude Pro token**: `make claude-token` (see "Claude plan token" below). It uses your current
   claude.ai login and stores a one-year token in `secrets/claude_oauth_token`. Keep `ANTHROPIC_API_KEY`
   and `ANTHROPIC_AUTH_TOKEN` unset.
4. **GitHub**: create a fine-grained token limited to the repos in `github.yaml`, with *Contents*,
   *Pull requests* and *Issues*: read/write, and *Metadata*: read. Save it to `secrets/github_token`.
   Protect `main` (rule R9). Then list your repos in `github.yaml` (see "GitHub intake" below).
5. **Langfuse**: create a project, put its keys in `.env.factory`.
6. **Postgres**: set `FACTORY_DB_PASSWORD` and `FACTORY_BOARD_DB_PASSWORD` in `.env.factory`, then
   `make db`, then `make db-board` (the board's read-only role). If your services are not named
   `postgres` and `langfuse-web`, rename them at the bottom of `compose.factory.yml`.
7. **Local model**: install a 4–8B instruct model with an MLX 4-bit build into your omlx models folder
   (one folder per model, folder name = the alias omlx serves). Bind omlx to `127.0.0.1:8000`.
8. `make hoststat OMLX_MODELS=/path/to/your/mlx/models` — installs the launchd memory sensor.
9. `make lock` (needs [uv](https://docs.astral.sh/uv/)) — writes hashed lockfiles. The images refuse to
   build without them.
10. `make runner-py DEPS=/path/to/target-repo` (or `runner-ts`) — bakes the target repo's dependencies
    into the runner image, because the runner has no network.
11. `make test-sandbox` — builds a test-only runner and proves, against your Docker, that runners are
    sealed (see "Isolation" below). Needs `pip install docker fastapi httpx` in the Python you use for
    `make test`. Do not go on until it passes.
12. `make up`, then `make logs`.
13. `make board` opens the factory board at http://localhost:8090. Keep it open: it lists what needs
    you and where every ticket is (see "Factory board" below).

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
interval (default 120 s, minimum 60), the two labels, and who may apply them.

**The labels explain themselves.** The factory creates its labels on each repo the first time it polls,
and keeps their colours and descriptions up to date. GitHub shows each description under the label name
in the label picker, so whoever triages issues sees what to add without reading this file:

| Label | Colour | Description in GitHub |
| --- | --- | --- |
| `factory:ready` | green | Add this to have the AI factory work on the issue. Needs write access. |
| `factory:approve` | purple | Add this to open the draft PR after the factory's tests pass. Needs write access. |
| `factory:awaiting-approval` | yellow | Tests passed. Add factory:approve to open the draft PR. |
| `factory:pr-open` | yellow | Draft PR open. Review and merge it; merging closes the issue. |
| `factory:halted` | red | Stopped. Read the factory's comment, then re-add factory:ready to retry. |
| `factory:queued`, `running`, `parked`, `rejected` | grey | Set by the factory, don't change by hand (`rejected` explains how to retry). |

Green and purple are the only labels a person adds. Yellow means it's your move, the same colour as on
the board.

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

## Factory board

`make board` opens **http://localhost:8090**: where every ticket is, and what you need to do next.

- **Your move** lists everything waiting on you, most urgent first: start the factory, approve a model,
  fix and retry a stopped ticket, approve a passed ticket, review and merge a PR, renew the Claude token.
  Each item says why, then gives the exact link (the GitHub issue, PR or Langfuse trace) or the command,
  with a Copy button.
- **The line** shows each ticket at its step: Queued, Triage, Plan, Tests, Code, Sandbox, Checks, Repair,
  Your approval, PR review. Blue tags are moving, dashed tags are paused on the Claude plan limit, red
  tags have stopped, and yellow tags are waiting for you.
- **Finished this week** lists merged PRs and tickets triage turned down, with the reason.

The board is **read-only**: it never approves or retries anything. A button on a local page cannot tell
who clicked it, and any other site open in your browser could trigger it, so approval stays on the
GitHub label (which checks who applied it) or `make approve`. The board tells you which one. It also:

- listens on `127.0.0.1` only, refuses requests addressed to any other host name, and sends a strict
  Content-Security-Policy;
- reads the database through `factory_board`, a role that can only `SELECT`;
- shows ticket text strictly as text, so an issue title can never run as code in your browser.

The board marks a ticket finished when its issue closes, which merging the PR does (`Closes #n`).

## Isolation: how the factory protects itself

Code the factory writes is treated as hostile, even when the ticket came from you: the model also reads
repo files and issue text you did not write. That code only ever runs in a **sealed runner** that
shares nothing with the factory. The factory's own code is baked into its images by `make up`, and
the factory refuses to work on its own repo, so a run can never change the factory.

### What lives where

| Part | Holds | Can reach | Never has |
| --- | --- | --- | --- |
| `orchestrator`, `pool-worker` | Your tokens (Docker secrets); git clones, frozen tests and results in the `factory-runs` volume; config files read-only | GitHub, Claude and allowlisted APIs through `egress-proxy`; Postgres; Langfuse; the broker | Target repo code running inside it; git hooks (every git call runs with hooks and `fsmonitor` off) |
| `sandbox-broker` | The fixed runner template | `docker-proxy` only | Tokens, host folders, the Docker socket, internet |
| `docker-proxy` | The Docker socket | Docker: container and volume calls only | `exec`, image, network, build or system calls |
| `runner-<run_id>` | A copy of the patched source (no `.git`), the frozen tests (read-only) | Nothing: `network=none` | Tokens, host folders, `.git`, the factory's clone; it is deleted after each run |

### One sandbox run, step by step

1. The factory applies the model's diff to its clone, then packs the source into an archive: regular
   files only, no `.git`, no symlinks, no `tests/`. The frozen tests go into a second archive.
2. The broker rebuilds both archives from scratch. Anything that is not a plain file or folder
   (a link, a device, `.git`, a path with `..`) makes it refuse the run.
3. The broker creates three throwaway Docker volumes (source, tests, results) and copies the archives
   in. Then it starts the runner with no network, a read-only root, no capabilities, 2 GB, 256
   processes and 300 seconds.
4. When the runner exits, the broker copies the results folder out. It keeps only the six known result
   files (`test.log`, `junit.xml`, `jest.json`, `lint.json`, `qa.json`, `result.json`), each a plain file
   under 1 MB. Everything else is dropped.
5. The broker deletes the runner and its volumes and returns the results as JSON. The factory writes
   them into its own folder and reads them from there. Before any text goes into a prompt, trace,
   GitHub comment or PR, it is scrubbed of the secret values the factory holds.

### The factory never changes itself

- Runs against `ethanoic/software-factory` are refused, from GitHub intake and from the inbox alike.
  If you fork the factory, set `FACTORY_SELF_REPOS` (comma-separated `owner/name`) in `.env.factory`.
- To change the factory: pull request, review, merge, `git pull` on the Mac, then `make up`. That
  rebuilds the images from the merged code. Nothing a run produces can reach those images.

### Checks to run

| When | Run | Expect |
| --- | --- | --- |
| After setup, and after every Docker Desktop update | `make test-sandbox` | All tests pass, none skipped. A skip means Docker or the test image is missing; a failure means a runner was not sealed. Do not start the factory until it passes. |
| Any time, especially after a crash | `make sandbox-check` | Both lists empty while no run is in progress |
| Weekly | `make runs-clean` (or `DAYS=30`) | Deletes clones of finished runs older than 14 days. Runs that are running, parked or awaiting approval are kept. |

### When something goes wrong

| You see | It means | Do this |
| --- | --- | --- |
| Halt: `sandbox environment: broker refused the run (400 rejected: …)` | The source or tests contained something other than plain files and folders, or were over 64 MB | Read the reason. A repo over 60 MB of source is too big for the sandbox; trim it or split it. |
| Halt: `sandbox environment: repository too large…` | Over 60 MB of source after the patch | Same as above |
| Halt: `refused: … is the factory's own repo` | A ticket pointed at the factory itself | Expected. Change the factory by pull request instead. |
| `make sandbox-check` lists containers or volumes when nothing is running | The broker stopped mid-run (crash, `make down`, Docker restart) | `docker rm -f $(docker ps -aq --filter label=factory.run_id)` then `docker volume rm $(docker volume ls -q --filter label=factory.run_id)`. A retry of the same run also cleans up after itself. |
| `docker-proxy` restarts in a loop, or runs halt with a broker error 500 | The proxy cannot reach the Docker socket | Docker Desktop → Settings → Advanced → allow the default Docker socket. If Enhanced Container Isolation is on, allow the `tecnativa/docker-socket-proxy` image to mount the socket, or turn it off. |
| A run halts because a package is missing | The runner has no network, so dependencies come only from the image | Add the package to the target repo, then `make runner-py DEPS=…` or `make runner-ts DEPS=…` and retry |

### If you think a run did something it should not

1. `make down`.
2. Revoke the GitHub token at github.com → Settings → Developer settings, make a new one, and save it
   to `secrets/github_token`. Run `make claude-token` to replace the Claude token.
3. On GitHub, check the target repos for unexpected `factory/*` branches, pushes and pull requests.
4. Open the run's Langfuse trace: each generate and repair step shows the exact diff the model wrote.
5. `make sandbox-check`, then `make test-sandbox`, then `make up`.

### Still open (do these by hand until fixed)

The sealed runner closes the review's two critical findings (F1 and F2). Three gaps remain:

- **A runner can still forge a pass.** It writes its own test report, so code that exits early or
  fakes `junit.xml` can look green. Before you approve, read the diff in the trace. Before you merge,
  read the draft PR's diff.
- **The diff filter allows test and tool config files** such as `conftest.py`, `jest.config.js` and
  `.npmrc`. Treat any change to those in a factory PR as a red flag.
- **Pushes may trigger the target repo's CI.** Approving pushes a `factory/*` branch. If that repo has
  workflows that run on every push with secrets, limit them to protected branches first.

## Daily use

- Open the board (`make board`) to see what needs you.
- Label issues `factory:ready` on GitHub, or drop a ticket JSON into `workspace/inbox/`
  (format in `workspace/inbox/README.md`).
- The factory alerts when a run **passes** (approve with the `factory:approve` label or
  `make approve RUN=<run_id>`), **halts** (reason + Langfuse trace link), or **parks** on plan limits
  (it retries hourly, up to 6 times).
- `make status` lists recent runs and spend.
- Weekly: `make runs-clean` and a glance at `make sandbox-check` (see "Isolation").

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
| Runner is sealed | `make test-sandbox` passes: no network, no `.git`, tests read-only, symlinked or oversized results dropped, nothing left behind |
| Runner has no network | a ticket whose test opens a socket to 1.1.1.1 must fail, not hang |
| Runner cannot see home | a test that lists `/Users` must fail |
| Frozen tests are tamper-proof | `make test-sandbox` shows the tests folder is read-only inside the runner; the factory also checks the frozen tests' hash after every run |
| Breaker trips before the call | set `RUN_CAP_USD=0.01` with a billed route → halt + alert, no provider call |
| 4th repair refused | a ticket whose tests cannot pass halts after 3 repairs |
| Local cost is $0.00 | Langfuse shows the triage generation at $0.00 with a `savings_usd` score |
| Plan limit parks the run | when Pro limits hit, the run moves to `inbox/parked/` and resumes later |
| Outsider labels are ignored | from an account without write access, add `factory:ready` to a test issue → one comment, no run |

## What has been tested, and what has not

Tested in this build (`make test`, 114 tests with `FACTORY_TEST_DSN` set and Docker running; 30 need
Postgres and 1 needs Docker):
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
sign-in and expiry reporting. A real `claude setup-token` sign-in has not been run. The board was run
against seeded Postgres through its read-only role and checked in Chromium at desktop and phone widths,
light and dark: no script errors, no sideways scrolling, and a hostile issue title rendered as text.

The sealed runner was run against a real Docker Engine 29 daemon on Linux: the factory's own sandbox
step called the live broker, which loaded a test runner through per-run volumes. Inside, the runner had
no network, no `.git`, no symlink from the repo, and a read-only tests folder. Its symlinked log,
2 MB lint file and nested file were dropped, and its containers and volumes were gone afterwards. A
`pre-commit` hook and `core.fsmonitor` planted in the factory's clone did not run during the factory's
git calls. The real runner images, `docker-proxy` and Docker Desktop on macOS were not available there.

Not yet exercised (Phase 0–1 on your Mac): building the runner, broker and proxy images, the sealed
runner on Docker Desktop through `docker-proxy` (`make test-sandbox` covers the runner, not the proxy),
the egress proxy, real Claude/omlx calls, Langfuse ingestion, and the real GitHub API (push, PR creation,
label and permission calls). Check in
particular:
- `host.docker.internal` resolving from the `egress-proxy` container on your Docker Desktop version.
- Langfuse SDK v3 span API and the public models API field names in `factory/obs.py` against your
  Langfuse server version.
- Lockfiles were generated for linux/arm64, Python 3.12 (`make lock` regenerates them).
