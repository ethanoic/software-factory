# Ticket inbox

Drop one JSON file per ticket here. `repo` must be a registered project (`make project-list`); the
project's language and branch are used whatever the file says. Every 30 seconds the factory takes the
next ticket: projects take turns (fewest runs in the last 24 hours first), each within its own daily
cap, and all within `subscription.max_factory_runs_per_day` while on Claude Pro.

```json
{"ticket_id": "APP-142", "title": "Cart rejects negative quantities",
 "body": "cart.add_item(sku, qty) accepts qty <= 0. It must raise ValueError and leave the cart unchanged.",
 "repo": "your-org/your-repo", "base_branch": "main", "language": "python"}
```

Subfolders: `processing/` (in flight), `done/`, `parked/` (waiting for Claude plan limits to reset),
`rejected/` (invalid JSON, or a repo that is not a project). Tickets for a paused project, or one
whose runner is not built yet, stay here until it is ready.
