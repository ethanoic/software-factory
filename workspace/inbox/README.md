# Ticket inbox

Drop one JSON file per ticket here. The orchestrator picks up the oldest file every 30 seconds,
up to `subscription.max_factory_runs_per_day` runs per 24 hours while on Claude Pro.

```json
{"ticket_id": "APP-142", "title": "Cart rejects negative quantities",
 "body": "cart.add_item(sku, qty) accepts qty <= 0. It must raise ValueError and leave the cart unchanged.",
 "repo": "your-org/your-repo", "base_branch": "main", "language": "python"}
```

Subfolders: `processing/` (in flight), `done/`, `parked/` (waiting for Claude plan limits to reset),
`rejected/` (invalid JSON).
