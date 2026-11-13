# AI Software Factory (local, M1 Max)

Turns a ticket into a draft pull request: **triage → plan → tests (written by a separate model) → check the
tests fail first → generate → sandbox → QA → independent review → repair (≤ 3) → human approval → draft
PR**. Claude (on your Pro plan) writes every line of code; a
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
| D1 | LangGraph + Postgres checkpointer; human approval before every PR; PRs open as drafts on `factory/<ticket>`. The pipeline code is the supervisor: no model decides which step runs next |
| D2 | Coding roles use Anthropic models only; which Claude model is chosen by your qualification suite, never hardcoded |
| D3 | Claude Pro via the Claude Agent SDK, signed in with a plan token from `make claude-token` (Docker secret), tools disabled |
| D4 | `local_mode: assist`: a 4–8B MLX model via omlx triages tickets; 10 GB resident cap |
| D5 | OpenRouter catalog for discovery and list prices only; no code goes through an aggregator; no LiteLLM |
| D6 | Model evals ≤ US$70/month billed (≈ SGD 90); Claude evals on Pro cost $0 but use plan limits |
| D7 | 3 repairs max; US$2/run and US$10/day for billed calls; 4 runs/day on Pro; 45 min per run |
| D8 | Sealed runner: no network, no shared folders, no `.git`, no tokens; code goes in and results come out as archives through the broker. Only `docker-proxy` holds the Docker socket. The factory never works on its own repo. Egress allowlist; hashed lockfiles |
| D9 | Many projects, one operator: each project is a row in the factory's database with its own runner image, daily cap and labelers; projects take turns. GitHub Issues polled every 2 min per project; only issues a maintainer labels `factory:ready`; status written back as labels and comments. `workspace/inbox/` stays as a fallback |
| D10 | The factory has its own Postgres. Langfuse is optional: one trace per run, explicit cost, `savings_usd` at list price |
| D11 | No model grades its own work: a separate test writer working from signatures, a "tests must fail first" check, and an independent reviewer that can block but never approve |

**Upgrade trigger (D3/D4):** if runs park on Pro limits more than twice a week, or the factory blocks
your own coding, move to Max 5x, claim its US$100/month API credit into a Console organization, set
`anthropic_route: api_key` and `ANTHROPIC_API_KEY`, and empty `secrets/claude_oauth_token`.

## Setup (Phase 0)

The factory is one service on your Mac with its own database. Clone this repo, then:

1. **Docker Desktop**: Settings → Resources → Memory 10 GB, swap 1 GB, CPUs 6. File sharing: remove
   `/Users`, add only this folder (rule R1). Runners never use file sharing; only the factory's own
   services read this folder (inbox, config, secrets). If you turn on Enhanced Container Isolation,
   allow the `tecnativa/docker-socket-proxy` image to mount the Docker socket, or `docker-proxy` cannot start.
2. `make init` — creates `workspace/`, `secrets/`, `backups/` and `.env.factory` (chmod 600) with random
   passwords for the factory's database. Docker must be running. `make up` repeats this check, so an
   older `.env.factory` gets any settings added since, and blank database passwords are generated
   before the database exists (never after: Postgres keeps its first password).
3. **Claude Pro token**: `make claude-token` (see "Claude plan token" below). It uses your current
   claude.ai login and stores a one-year token in `secrets/claude_oauth_token`. Keep `ANTHROPIC_API_KEY`
   and `ANTHROPIC_AUTH_TOKEN` unset.
4. **GitHub token**: create a fine-grained token for the repos the factory will work on, with *Contents*,
   *Pull requests* and *Issues*: read/write, and *Metadata*: read. Save it to `secrets/github_token`.
   Protect each repo's `main` (rule R9). Adding a repo later means adding it to this token too.
5. **Local model** (optional, for triage): install a 4–8B instruct model with an MLX 4-bit build into
   your omlx models folder, bind omlx to `127.0.0.1:8000`, then `make hoststat OMLX_MODELS=/path/to/models`.
   Without one, set `local_mode: off` in `pool.yaml`.
6. `make lock` (needs [uv](https://docs.astral.sh/uv/)) — writes hashed lockfiles. The images refuse to
   build without them.
7. `make test-sandbox` — proves, against your Docker, that runners are sealed (see "Isolation" below).
   Needs `pip install docker fastapi httpx` in the Python you use for `make test`. Do not go on until it
   passes.
8. `make up` — starts the factory and its database (created on the first start), then `make logs`.
9. **Add your first project**: `make project-add REPO=owner/name LANGUAGE=javascript` (see "Projects").
10. **Approve a model**: `make pool ARGS="refresh"`, then `make pool ARGS="trust <model_id>"` (see
    "Qualify before the first ticket").
11. `make board` opens the factory board at http://localhost:8090. Keep it open: it lists what needs
    you and where every ticket is in every project (see "Factory board" below).

Langfuse traces are optional; see "Optional: Langfuse" below.

## Projects

A project is a GitHub repo the factory works on. Projects live in the factory's database, and the
factory refuses tickets for any repo that is not one, so a stray ticket file or a typo cannot point it
at the wrong code.

| Command | What it does |
| --- | --- |
| `make project-add REPO=owner/name LANGUAGE=node [BRANCH=main] [MAX_RUNS=2]` | Registers the repo, then builds its test runner. Languages: `python`, `node` (JavaScript or TypeScript; `javascript` and `typescript` still work), `rust`, `java`, `godot`. |
| `make project-list` | Every project: active or paused, language, branch, daily cap, runner image |
| `make project-rebuild REPO=owner/name` | Rebuilds the runner from the repo's current dependency files |
| `make project-rebuild-all` | Rebuilds every project's runner; do this after updating the factory |
| `make project-pause REPO=…` / `make project-resume REPO=…` | Stops or restarts taking that project's tickets; queued tickets wait |
| `make project-limit REPO=… N=2` (or `N=none`) | At most N runs a day for this project |
| `make project-labelers REPO=… LOGINS='alice bob'` | Extra GitHub logins allowed to label this project's issues |
| `make project-remove REPO=…` | Forgets the project; its run history stays on the board |

**Each project has its own test runner.** `project-add` and `project-rebuild` ask the factory to clone
the repo (inside its container, with git hooks off). The factory hands back only the dependency files
(see the table below). An image named `factory-runner-<project>:<hash of those files>` is built from
them, so two projects never share packages, and nothing is downloaded while tests run.

| Language | Dependency files baked into the image | Tests (frozen, in `tests/`) | Lint on changed files | Security scan |
| --- | --- | --- | --- | --- |
| `python` | `requirements*.txt` | pytest, `tests/test_*.py` | ruff | bandit |
| `node` | `package.json`, `package-lock.json` | Jest, `tests/*.test.js` (repo's own Jest) | repo's ESLint config, if any | eslint-plugin-security |
| `rust` | `Cargo.toml`, `Cargo.lock` (crates vendored and compiled once) | cargo-nextest, `tests/*.rs` | clippy (deny-level only) | none |
| `java` | `pom.xml` (resolved offline into the image) | Maven Surefire, `tests/*Test.java`, compiled alongside `src/test/java` | none (compile errors only) | none |
| `godot` | `.godot-version` (Godot release, 4.3 or later; default 4.4.1) | GUT, `tests/test_*.gd`; GUT must be committed at `addons/gut/` | gdlint | none |

Limits of the new runners: one Rust crate at the repo root (no Cargo workspaces yet), Maven only (no
Gradle yet), and no security scanner for Rust, Java or GDScript. For those three the independent
reviewer is the only check for unsafe code, so keep `review.mode: block`. A GUT test that never
reaches an assertion (in GDScript, calling a function that does not exist is a run-time error, not a
compile error) counts as failed, and any `SCRIPT ERROR` fails the run. The `examples/hello-rust`,
`examples/hello-java` and `examples/hello-godot` folders are small projects to try each runner on.

**Dependency changes are caught before any model call.** Every run starts by hashing the repo's
dependency files. If they differ from what the runner was built with, the run stops with
`dependencies changed since its runner image was built` and the exact `make project-rebuild` command.
The board shows the same command.

**Projects take turns.** The factory runs one ticket at a time. When several projects have tickets
waiting, the one with the fewest runs in the last 24 hours goes next (ties: the oldest ticket). A
project at its own daily cap waits while others continue. The overall Claude plan limit
(`max_factory_runs_per_day` in `pool.yaml`) still applies on top.

**What stays shared:** your Claude token, the model pool (qualified models serve every project), the
GitHub token, and the cost caps per run and per day.

**Moving from an older setup:** repos still listed under `repos:` in `github.yaml` become projects on
the next `make up`. Build their runners with `make project-rebuild-all`, then delete the `repos:` list.

## The factory's database

Postgres runs inside the factory (`postgres` service, `factory-db` volume). It is reachable only on the
factory's internal network, never from your Mac's ports. It holds runs, projects, the model pool, the
cost ledger and GitHub issue status. It is created on the first `make up`, and its tables are updated
automatically when the factory starts.

| Command | What it does |
| --- | --- |
| `make backup` | Writes `backups/factory-<date>.sql.gz` (mode 600) |
| `make restore FILE=backups/…` | Stops the factory, replaces the database with that backup, starts it again |
| `make diff RUN=<run_id>` | Prints the change a run made, from the database; read it before you approve |

Back up before updating the factory. `docker compose down -v` deletes the database volume; `make down`
does not.

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

The factory polls every active project that has a runner (see "Projects"). `github.yaml` holds the
settings shared by all of them: the poll interval (default 120 s, minimum 60), the two labels, and who
may apply them. `make project-labelers` adds people for one project only.

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

Set `enabled: false` in `github.yaml` to run on the inbox folder only. Inbox tickets must name a
registered project in `repo`; others are moved to `workspace/inbox/rejected/`.

## Optional: Langfuse

Off by default: the factory runs fully without it, and `make diff` shows each run's change. To see a
trace per run (every model call, token counts, cost), run Langfuse yourself, for example its own Docker
Compose setup, then set these in `.env.factory` and run `make up` again:

```bash
LANGFUSE_DISABLED=0
LANGFUSE_HOST=http://host.docker.internal:3000      # where the factory reaches it
LANGFUSE_PUBLIC_URL=http://localhost:3000           # where your browser opens traces
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Langfuse keeps the full prompts, including issue text and source files. Keep it on this Mac.

## Factory board

`make board` opens **http://localhost:8090**: where every ticket is, and what you need to do next.

- **Projects** lists each project: ready, stopped tickets, no runner yet, or paused, with its runs today.
  Click a project, or use *Show*, to see only its tickets and the items waiting on you for it; items
  about the factory itself (start it, approve a model, renew the token) stay visible either way. The
  address becomes `http://localhost:8090/?project=owner/name`, so you can bookmark one project's view.

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
| A run halts because a package is missing | The runner has no network, so dependencies come only from the image | Add the package to the target repo, then `make project-rebuild REPO=owner/name` and retry |

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
  fakes `junit.xml` can look green. The independent reviewer now blocks the usual ways of doing this
  (`obfuscation`, `disabled_checks`), but it reads code and can miss things. Before you approve, read
  `make diff`; before you merge, read the draft PR's diff.
- **The diff filter allows test and tool config files** such as `conftest.py`, `jest.config.js` and
  `.npmrc`. Treat any change to those in a factory PR as a red flag.
- **Pushes may trigger the target repo's CI.** Approving pushes a `factory/*` branch. If that repo has
  workflows that run on every push with secrets, limit them to protected branches first.

## Independent checks: no model grades its own work

The model that writes the code never decides whether it is done. Three checks sit between it and your
approval, and the pipeline code (not a model) decides what happens after each.

| Check | Who | Sees | Outcome |
| --- | --- | --- | --- |
| **Tests written separately** | The `write_tests` role: a different model from the code writer whenever one is approved | The ticket, the plan's acceptance criteria and the target files' **signatures only** (function and class names, parameters, docstring first lines), never their code | New test files under `tests/`, with every criterion mapped to at least one test, or the run stops |
| **Tests must fail first** | No model: the sealed runner | The new tests against the **unchanged** code | At least one must fail. If all pass (or none ran), the test writer gets one more try, then the run stops: tests that already pass do not test the change |
| **Independent review** | The `review_code` role: a different model from the code writer whenever one is approved | The ticket, criteria, the diff and the test count, never the writer's reasoning | Findings by category. The factory, not the model, decides which block (`review` in `pool.yaml`) |

**What the reviewer blocks, by default:** `scope` (changes the ticket did not ask for), `secrets`
(reading tokens, env vars or files outside the repo), `network` (calls, sockets, downloads, shell
commands), `disabled_checks` (weakened tests, validation or auth), `test_or_tool_config` (test, lint,
build, package or CI config), and `obfuscation` (`eval`, encoded code, behaving differently under test).
A block sends the change back to repair, counting toward the 3-repair limit; security categories go to
the `security_fix` role. `unmet_criterion`, `correctness` and `quality` findings never block: they appear
in the approval comment, the board, `make diff` and the PR body. The reviewer **cannot approve**
anything, and an answer it gives that cannot be read is shown to you as "did not complete", never as a pass.

**Use two Claude models.** Independence needs a second approved model. With one, the factory still runs
both checks and says on every approval that the same model reviewed its own code. To add a second:

```bash
make pool ARGS="trust anthropic/<a second Claude model id> write_tests review_code"
```

Set `review.independence: require` in `pool.yaml` to stop runs instead when no second model is approved,
and `review.mode: advise` to see findings without ever blocking.

**The reviewer is qualified on its own suite.** `qualification/review/` holds diffs it must judge: known-bad
ones it must block for the right reason, and clean ones it must let through (see
`qualification/README.md`). The six starter diffs use the hello-node example; add some in your own
projects' style. The test writer is measured with the code suite, as part of the whole pipeline.

**Cost:** two to three more Claude calls per run (the test writer, the reviewer after each green run, and
at most one test rewrite). The "fail first" check costs one extra sandbox run and no model calls.

## Daily use

- Open the board (`make board`) to see what needs you.
- Label issues `factory:ready` on any project's repo, or drop a ticket JSON into `workspace/inbox/`
  (format in `workspace/inbox/README.md`).
- Before approving, read the change: `make diff RUN=<run_id>` (the board gives the command).
- When a project adds or changes a dependency: `make project-rebuild REPO=owner/name`.
- Weekly: `make backup`.
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

Tested in this build (`make test`, 222 tests with `FACTORY_TEST_DSN` set and Docker running; 39 need
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

The independent checks were tested without a real model: reading which new tests failed from **real**
pytest and Jest output (including a new file that cannot import, and pytest stopping at a collection
error), the test writer's rules (new files only, every criterion covered), signature extraction hiding
function bodies, the reviewer's categories decided by the factory and not the model, an unreadable review
shown as incomplete, model independence (a different model when one is approved, flagged or stopped when
not), the pipeline's new routes (a review block goes to repair, a review that never clears halts at the
limit, passing tests go back to the test writer once), and the reviewer suite's scoring. Not exercised:
real Claude answers for the two new roles, and the JavaScript signature reader on large or unusual
TypeScript files (it reads declarations line by line).

Projects were tested against Postgres 16: the registry and its refusals, moving `github.yaml` repos
over, fair turns (a busy project cannot starve a quiet one), per-project caps, the checks at the start
of a run (unregistered, paused, no runner, project settings override the ticket, dependency drift
caught after a real clone), and unregistered inbox tickets rejected before a run. `scripts/project.sh`
was run end to end with the real factory code, Postgres and a real git clone, with `docker` replaced by
a recorder: it fetched only the dependency files, built from the right context and registered the image.
The database's first-start script was run against Postgres 16: the board role reads tables created by
later migrations and cannot write. The board was checked in Chromium with three projects, at desktop and
phone widths, light and dark: no script errors, no sideways scrolling, and the project filter works.

The Rust, Java and Godot runners (13 Nov 2026) were run through the real entrypoint, outside Docker,
with the factory's own result readers judging each run. Rust (cargo-nextest 0.9, clippy) and Godot
(4.4.1 headless, GUT 9.4, gdlint) on the `examples/` projects: a new test calling missing code fails the
tests-fail-first check, wrong code is an assertion failure, right code passes, a syntax error and a lint
error are repairable, a panic fails the test, and missing tooling (no GUT, no nextest, a crate that is not
vendored) halts instead of starting a paid repair. A crate with a real dependency was vendored and
pre-compiled the way the Dockerfile does, then tested with the network blocked: nothing was downloaded
or recompiled. The Python and Node paths gave the same results as before, including through an image
that does not declare its runner. Not run: the three new Dockerfiles (Docker Hub was not reachable),
and Java end to end (Maven Central was not reachable): its report reading and compile-error handling are
tested on Surefire's format, and its offline failure on real Maven output.

The sealed runner was run against a real Docker Engine 29 daemon on Linux: the factory's own sandbox
step called the live broker, which loaded a test runner through per-run volumes. Inside, the runner had
no network, no `.git`, no symlink from the repo, and a read-only tests folder. Its symlinked log,
2 MB lint file and nested file were dropped, and its containers and volumes were gone afterwards. A
`pre-commit` hook and `core.fsmonitor` planted in the factory's clone did not run during the factory's
git calls. The real runner images, `docker-proxy` and Docker Desktop on macOS were not available there.

Not yet exercised (Phase 0–1 on your Mac): building the runner, broker and proxy images and a
project's runner image, the `postgres` service and `make backup` / `make restore` inside Docker, the sealed
runner on Docker Desktop through `docker-proxy` (`make test-sandbox` covers the runner, not the proxy),
the egress proxy, real Claude/omlx calls, Langfuse ingestion, and the real GitHub API (push, PR creation,
label and permission calls). Check in
particular:
- `host.docker.internal` resolving from the `egress-proxy` container on your Docker Desktop version.
- Langfuse SDK v3 span API and the public models API field names in `factory/obs.py` against your
  Langfuse server version.
- Lockfiles were generated for linux/arm64, Python 3.12 (`make lock` regenerates them).
