# factory-hello-java

A deliberately tiny Maven project for trying the AI Software Factory. It has one class, `Greeter`, and
JUnit 5 tests in `src/test/java`.

```bash
mvn test          # 2 passing tests
```

Rules the factory enforces on this repo:

- The factory writes its new tests as `tests/<Name>Test.java`. They are frozen for each run, and the runner
  compiles them alongside `src/test/java`. Existing tests stay where they are.
- `pom.xml` and `.github/` are never changed by the factory. Add dependencies yourself, then rebuild the
  runner image (`make project-rebuild REPO=<this repo>` in the factory folder).
- Maven only. Gradle builds are not supported yet.
