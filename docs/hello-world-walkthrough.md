# Hello world: your first factory run

This walkthrough takes the tiny Node.js app in [`examples/hello-node`](../examples/hello-node) from a GitHub
issue to a merged pull request written by the factory. You will put the app in its own repository, point the
factory at it, approve a Claude model, and file three issues: two the factory should complete and one it
should refuse.

**Before you start:** finish [README → Setup (Phase 0)](../README.md#setup-phase-0) steps 1–9: Docker
limits, your Claude token (`make claude-token`), Langfuse keys, the factory database and the lockfiles. Commands
below run from the factory folder unless they say otherwise.

## 1. Put the example in its own repository

The factory clones the repository it works on. Keep the example separate from the factory repo, so the
factory never edits its own code.

```bash
cp -R examples/hello-node ~/factory-hello-node
cd ~/factory-hello-node
git init -b main && git add -A && git commit -m "Hello-world target for the factory"
gh repo create ethanoic/factory-hello-node --public --source . --push
```

No `gh` CLI? Create an empty repository named `factory-hello-node` on github.com, then run
`git remote add origin https://github.com/ethanoic/factory-hello-node.git && git push -u origin main`.

Then protect its `main` branch the way the factory repo is protected (Settings → Branches): require a pull
request, 0 approvals, and no bypass for admins. The factory pushes only `factory/*` branches, so this costs it
nothing. On GitHub Free, branch protection works on public repositories only.

## 2. Give the factory access to the new repository

Edit the fine-grained token in `secrets/github_token` (GitHub → Settings → Developer settings → Fine-grained
tokens) so its repository access includes `factory-hello-node`, with these permissions:

| Permission | Access | Used for |
| --- | --- | --- |
| Contents | Read and write | cloning, pushing `factory/*` branches |
| Pull requests | Read and write | opening the draft PR |
| Issues | Read and write | reading issues, setting labels, posting status comments |
| Metadata | Read | checking who applied a label |

## 3. Build the Node runner image

Tests run in a container with no network, so the app's dependencies (Jest, ESLint) are installed into the
image ahead of time from its lockfile:

```bash
make runner-ts DEPS=examples/hello-node
```

There is one Node runner image at a time. Rebuild it whenever the app's `package.json` changes, or when you
point the factory at a different Node repository.

## 4. Point the factory at the repository

Replace the `repos` list in `github.yaml`:

```yaml
enabled: true
poll_interval_s: 120
trigger_label: "factory:ready"
approve_label: "factory:approve"
labelers: []
allowlist_only: false
comment_on_status: true
repos:
  - repo: ethanoic/factory-hello-node
    base_branch: main
    language: javascript
```

## 5. Start the factory and approve a model

```bash
make up
make pool ARGS="refresh"          # discover models; prints the catalog
```

Normally no model may work until it passes your qualification suite of 10–20 solved tickets (decision D2).
For this walkthrough, approve one by hand instead:

```bash
make pool ARGS="trust anthropic/<a Claude model id from the catalog>"
make pool ARGS="trust local/<your MLX model id> classify"     # only if you installed a local model
make pool ARGS="list"                                          # every role should show a model
```

- **Which Claude model:** for the hello world, pick a mid-tier (Sonnet-class) model. Every run uses your Pro
  usage limits.
- **No local model yet?** Set `local_mode: off` in `pool.yaml` and run `make up` again. The trusted Claude
  model then triages tickets too.
- **What trust means:** the model is marked *trusted (not measured)*. The first real qualification result
  replaces it, and changing files in `qualification/` makes it inactive (`list` shows `[suite changed]`).
  Write real suites before Phase 3 of the rollout.

## 6. Watch it start

```bash
make logs
```

Look for `GitHub intake: ethanoic/factory-hello-node`. On its first poll the factory creates its labels on the
repository (`factory:ready`, `factory:approve` and the status labels), so they appear under Issues → Labels.
Each has a description saying what it does, which GitHub shows in the label picker: green and purple are
the two you add; the rest are set by the factory.

Then open the board, and keep it open for the rest of the walkthrough:

```bash
make board        # http://localhost:8090
```

**Your move** at the top lists what is waiting on you, with the link or command for each step. **The line**
below it shows where every ticket is. If the factory is not running or no model is approved, the first item
says so and gives the command.

## 7. Create the issues

The repository has an issue form: **Issues → New issue → Factory task**. It asks for the change and concrete
acceptance criteria, which the factory turns into tests before it writes code. Creating an issue does
**not** start a run; adding the `factory:ready` label does.

Create these three. Run the commands from `~/factory-hello-node`, or paste the text into the web form.

**Issue A: should succeed**

```bash
gh issue create --title "greet() should trim whitespace and treat blank names as missing" --body "$(cat <<'EOF'
### What should change
greet() should ignore whitespace around the name. A name made only of whitespace counts as no name.

### Acceptance criteria
- greet("  Ada ") returns "Hello, Ada!"
- greet("   ") returns "Hello, World!"
- greet() still returns "Hello, World!"

### Files likely involved
src/greet.js
EOF
)"
```

**Issue B: should succeed**

```bash
gh issue create --title "Add farewell(name)" --body "$(cat <<'EOF'
### What should change
Add a farewell(name) function next to greet(), exported from src/greet.js.

### Acceptance criteria
- farewell("Ada") returns "Goodbye, Ada!"
- farewell() returns "Goodbye, World!"
- greet() behaves exactly as before

### Files likely involved
src/greet.js
EOF
)"
```

**Issue C: should be refused**

```bash
gh issue create --title "Rotate the API key used by the CI pipeline" \
  --body "The CI pipeline's API key may have leaked. Rotate it and update the stored value."
```

Now hand **Issue A only** to the factory: open it, then **Labels → factory:ready** (or
`gh issue edit 1 --add-label factory:ready`). Start with one issue: runs go one at a time, and on Claude Pro
the factory takes at most 4 runs a day. Label B and C once A has finished.

The label only counts when someone with write access applies it. You own the repository, so yours does.

## 8. What happens next

The board shows each of these steps as it happens; the issue labels show the same thing on GitHub.

| When | On the issue | What it means |
| --- | --- | --- |
| Within 2 minutes | `factory:ready` → `factory:queued` | The factory took the issue |
| Shortly after | `factory:running`, comment with the run id | Triage, planning, tests, code, sandbox. Follow it live in Langfuse at http://localhost:3000 |
| A few minutes later | `factory:awaiting-approval`, comment | The new tests pass and the security scan is clean |
| You add `factory:approve` | `factory:pr-open`, comment with the PR link | A draft PR from branch `factory/ethanoic-factory-hello-node-1` that says `Closes #1` |
| You merge the PR | Issue closes | Done |

**Review the PR before merging:** expect a small change to `src/greet.js` plus a new test file under
`tests/` that encodes your acceptance criteria. Click **Ready for review**, then merge.

**Issue C** ends as `factory:rejected` with a comment giving the reason. It is refused before any model is
called, because it mentions an API key and a CI pipeline.

## 9. Without GitHub issues: the inbox

To try a task without filing an issue, save it as a JSON file in `workspace/inbox/`:

```json
{"ticket_id": "HELLO-1", "title": "Add farewell(name)",
 "body": "Add farewell(name) next to greet() in src/greet.js. farewell(\"Ada\") returns \"Goodbye, Ada!\"; farewell() returns \"Goodbye, World!\".",
 "repo": "ethanoic/factory-hello-node", "base_branch": "main", "language": "javascript"}
```

Inbox tickets report through alerts and `make logs` instead of issue labels. Approve one with
`make approve RUN=<run_id>`.

## Writing tickets the factory can finish

- **Small:** 3 files or fewer. Larger changes are marked complex and cost more.
- **Concrete:** list input → output examples. Vague tickets end at the repair limit.
- **Code only:** the factory never edits `package.json`, lockfiles, `tests/` or `.github/`. Add dependencies
  yourself, then rebuild the runner image (step 3).
- **Avoid trigger words:** tickets mentioning secrets, passwords, credentials, API keys, migrations,
  infrastructure, Dockerfiles, Terraform, Kubernetes, Helm, CI pipelines or `.github/workflows` are refused
  automatically, because those changes need a human.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `factory:ready` stays on the issue | Not polled yet, intake off, or the token lacks *Issues* access | Wait 2 minutes; check `make logs` for the `GitHub intake:` line; recheck step 2 |
| Comment "…lacks write access" | Someone without write access applied the label | Re-apply it yourself |
| `factory:queued` but nothing starts | The daily limit of 4 runs on Pro is reached | Waits for the 24-hour window; see `subscription.max_factory_runs_per_day` in `pool.yaml` |
| Halted: "no qualified model for role …" | Step 5 not done, or `qualification/` changed | `make pool ARGS="list"`, then trust again |
| Halted: "sandbox environment" | The runner image is missing a dependency, or Jest found no tests | Rebuild with step 3; re-add `factory:ready` |
| Halted: "repair limit reached" | The ticket was ambiguous, or the change was too big | Sharpen the acceptance criteria, then re-add `factory:ready` |
| `factory:parked` | Claude Pro usage limit reached | Nothing to do: the run resumes on its own (hourly, up to 6 tries) |
| Rejected, but the ticket is legitimate | It contains a trigger word | Reword it; see "Writing tickets" above |
| Triage stops working after a night | The nightly check measured your trusted model against `qualification/triage/` and it failed | `make pool ARGS="list"`; fix or extend the triage examples, then trust again |
