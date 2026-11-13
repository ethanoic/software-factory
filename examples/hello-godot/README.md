# factory-hello-godot

A deliberately tiny Godot 4 project for trying the AI Software Factory with GDScript. It has one script,
`scripts/greeter.gd` (`Greeter.greet(name)`), and GUT tests in `tests/`.

Before the factory can run it, add the [GUT](https://github.com/bitwes/Gut) addon (9.x for Godot 4) to
`addons/gut/` and commit it. The factory's runner has no network, so it uses the copy in the repo.

```bash
godot --headless --import
godot --headless -s res://addons/gut/gut_cmdln.gd -gdir=res://tests -gexit   # 2 passing tests
```

Rules the factory enforces on this repo:

- `tests/` is frozen for each run: the factory writes new tests during planning and cannot edit them afterwards.
- `.godot-version`, `addons/gut/` and `.github/` are never changed by the factory.
- `.godot-version` picks the Godot release the runner downloads (4.3 or later). Change it yourself, then
  rebuild the runner image (`make project-rebuild REPO=<this repo>` in the factory folder).
