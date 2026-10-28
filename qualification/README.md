# Qualification suites

The pool worker scores every discovered model against these suites before it can fill a role.
Changing any file here changes the suite hash, so all models are re-scored on the next cycle.

## `code/` — plan, generate_remote, repair, security_fix (and local `generate`)

One JSON file per task, taken from a real, already-solved ticket in a repo the factory can clone.
A task passes when the full pipeline reaches the PR step (dry run, nothing pushed) with **zero**
repair cycles. Aim for 20 tasks; 10 is the minimum for Phase 1.

```json
{"ticket": {"ticket_id": "QUAL-001", "title": "Reject negative quantities in cart",
            "body": "Adding an item with quantity < 1 must raise ValueError. ...",
            "repo": "your-org/your-repo", "base_branch": "qual-base-001", "language": "python"}}
```

Use a branch (`base_branch`) that points at the commit **before** the real fix, so the task stays
reproducible.

## `triage/` — classify (local) and spillover (cheapest Claude)

One JSON file per labelled ticket. 50 tickets for the Phase 3 gate (≥ 90% agreement).

```json
{"ticket": {"ticket_id": "TRI-014", "title": "Rotate the staging DB password",
            "body": "...", "repo": "your-org/your-repo", "language": "python"},
 "expected_accept": false}
```

Include both kinds: tickets you would hand to the factory, and ones you would not (infra, vague,
cross-cutting). The examples in this folder are templates; replace them with your own.
