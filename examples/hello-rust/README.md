# factory-hello-rust

A deliberately tiny Rust library for trying the AI Software Factory. It has one function,
`greet(name)`, and integration tests in `tests/`.

```bash
cargo test        # 2 passing tests
cargo clippy
```

Rules the factory enforces on this repo:

- `tests/` is frozen for each run: the factory writes new tests during planning and cannot edit them afterwards.
- `Cargo.toml`, `Cargo.lock` and `.github/` are never changed by the factory. Add dependencies yourself,
  then rebuild the runner image (`make project-rebuild REPO=<this repo>` in the factory folder).
- One crate at the repo root. Cargo workspaces are not supported yet.
